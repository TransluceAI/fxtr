# Stage 3 — Sessions on `/client` (no callbacks)

Goal: extend the `/client` protocol with the full session-management spec from `../sketch.md`, *minus* callback support. After this stage the agent MCP and full UI clients should both be able to drive workstation state via sessions, even without callbacks.

## Scope

- New session resource on `WorkstationState`: UUID, JSON state, event log, slot map (for invocation/load results), tracker subset (entities + services), subscriber list.
- `/client` operations introduced:
  - `create_session(metadata)` / `close_session(...)` (close is itself a mutation op — see 3b).
  - `subscribe_session(session_id)` / `unsubscribe_session(subscription_id)`. Subscribe returns initial state snapshot + replays unacknowledged events.
  - `mutate_session(subscription_id, ack_seq, update_id, json_patch, ops?)` — the ACID-style transaction described in the part-0 sketch. `ops` includes: track/untrack entity, track/untrack service, "load" an entity into a slot, "invoke" a service method into a slot, "close" the session. Callbacks are deferred to stage 5.
  - `list_sessions(filter_metadata?)` / `subscribe_session_list(filter_metadata?)`.
  - Existing `subscribe(resource=...)` from stage 2 is extended: tracked-entity / tracked-service records gain `pinned_by_sessions: [uuid, ...]` (see 3d).
- Event log per session: append-only sequence of `(seq, event)` records covering state updates and result notifications. Per-session monotonic counter.
- Slot machinery: each session has a strictly-increasing integer slot counter; "load entity" and "invoke service method" allocate the next slot and asynchronously fill it; the result event lands in the event log against that slot.
- Cross-session resource sharing: tracking ops accept short IDs from anywhere in the workstation, per the part-0 sketch.
- Session shutdown rules: closing a session releases all its trackers; unsubscribing a connection from a session does NOT close the session.

## Resolved design decisions

These were settled in the design discussion before fleshing out the plan; recorded here so the spec is self-contained.

- **"Same client" for stale-ack mutation conflicts** = `(connection_id, subscription_id)` pair. A reconnect or a different subscription on the same connection counts as a distinct client.
- **Slot numbering**: client-allocated. Mutations specify the slot number for each load/invoke op. Slot numbers must be strictly larger than any slot ever used in the session. If two subscriptions race to use the same slot, exactly one mutation succeeds (the other one's stale-ack check fails on a different ground first, but the slot constraint is also a backstop).
- **Mutation success response** to the originator is *distinct* from the `state_update` event other subscribers receive. The mutator gets a dedicated `mutation_committed` response carrying `{request_id, seq, update_id}` and does **not** receive a `state_update` `session_event` for their own commit. Rationale: the mutator already has the post-state (they computed the patch); echoing the full state is wasted bandwidth, and the semantic outcomes ("you committed, advance" vs "someone else moved state, retry") are different enough that forcing them through one shape muddies client logic. Other subscribers — including different subscriptions on the same connection — see the foreign-update path. This also lets us preserve the sketch's rule that foreign `state_update` events do not carry the originator's identity.
- **Load-entity result encoding**: server loads the CBOR dict, recursively rewrites any `FxtrID`-typed fields into `["entity", "<short-id>"]` tags, and any `ServiceRef`-typed fields into `["service", "<short-id>"]` tags. (See "List-based escape format" below.) Each child encountered this way is workstation-tracked (gets a short ID and a tracker) at delivery time, and stays tracked until the subscription that owns the slot acks the result event into state via a subsequent mutation.
- **List-based escape naming.** The wire format was inspired by Cap'n'Web's escape rules but we don't adopt the rest of Cap'n'Web; in code the name is "list-based escape" everywhere (module: `wire_escape.py`; functions: `escape` / `unescape`). A one-line credit at the top of the module is the only place "Cap'n'Web" appears in the codebase.
- **Three-phase delivery pipeline for slot results.** Every load/invoke delivery runs as: (1) sync per-ref attach (under session-alive gate), (2) async wait on `bridge_ready` for newly-created service records, (3) sync commit. Refcount mutations are sync; phase 2 is the only async portion and it only awaits events. This composition derives from the invariants below — see "Invariants and primitives".
- **Refcount-driven `tracking_sessions`, no orphan-record cleanup.** The two channels (explicit pins + retained-event refs + in-flight delivery contributions) collapse into one refcount dict per session. `session ∈ TrackedXRecord.tracking_sessions` ⟺ `session.x_refcounts[key] > 0`. Because phase-1 attach happens entirely sync and only after a session-alive check, a record never exists in the registry without at least one holder. Session-close is the single cleanup actor for session-side refs; we don't need a separate orphan-record sweep.
- **Mutation atomicity**: full atomicity. If any op fails validation or any patch op fails, the entire mutation is rejected, no slots are consumed, no state-update event is appended, and no subscribers see the change.
- **Slot lifetime**: load/invoke slots never explicitly disappear; once their result event is acked into state by some mutation (i.e. mutation arrives with `ack_seq` past that result event's seq), the server can drop the slot's payload and the result event itself from the log. The slot number remains "consumed" (cannot be reused) but the in-memory record is freed.
- **Event-log retention**: keep events with `seq >= ack_seq_of_last_accepted_state_update`. Anything older has either been folded into state by the mutator or will never be delivered to a fresh subscriber. (No durable log for phase 0.)
- **Session metadata**: settable at `create_session(metadata)`, immutable thereafter. Anything mutable lives in `state`.
- **Closing a session**: implemented as a `close` op inside a regular mutation (so the closer must be ack-up-to-date). On close: workstation drops its handles to in-flight load/invoke results; the underlying calls keep running (the deliver task discards the result if the owning session is gone), all session trackers are released, all subscribers receive a `closed` event. After close, `subscribe_session(uuid)` returns an error.
- **Subscription resync**: re-subscribing always returns to the fresh-snapshot path (subscribe returns the most recent state-update event + the suffix of events past its `ack_seq` + next available slot). If the client did a final `mutate` before dropping the connection, that mutation's `ack_seq` advances the retention horizon, so the suffix is small.
- **JSON-Patch dialect**: full RFC 6902 (`add`, `remove`, `replace`, `move`, `copy`, `test`).
- **Session-source registrations and RPC connection lifetime** (3b.3): a session that tracks a service via `TrackServiceOp` keeps that service alive *by keeping the registering RPC connection alive*. The workstation's bridge in `service_scope` pins the dispatch scope of the original `register_service` call, which is a child of the registering connection's `conn_scope`. A graceful RPC-connection close therefore *blocks* until the session releases (untrack or session close), mirroring structured-concurrency: outstanding work owns the lifetime of its supports. The session is the responsible party for cleanup. A *cancelled* or *crashed* RPC connection (async-task-level cancel, transport error) bypasses the wait and forces tear-down — but graceful drains do not. This semantic is by design and applies to services only; entity registrations are independent because `copy_entity` copies the data into the workstation-owned `entities_scope` rather than bridging.

## Wire protocol changes

### Frame additions (msgspec tagged unions)

Client → server requests (extends the `op` discriminator):

- `create_session` — `{id, op, metadata: dict}`. Replies with an `ack` carrying the new session UUID. (Define a new `CreateSessionResponse` ack variant or fold the UUID into `AckResponse`'s payload — TBD during 3a; preference is a new `created_session` response variant for clarity.)
- `close_session(...)` — *not a top-level op*; closing is a mutation op (see below).
- `subscribe_session` — `{id, op, session_id}`. The request `id` is the subscription id, mirroring stage-2 conventions. Replies with `ack` then a `session_snapshot` event.
- `unsubscribe_session` — `{id, op, subscription_id}`. Replies with `ack`.
- `mutate_session` — `{id, op, subscription_id, ack_seq, update_id, patch, ops?}`. See "Mutation semantics" below. Reply paths: a `mutation_committed` response (success — see below) or `error` (rejected — stale ack, validation error, op error). The mutator does **not** also receive a `session_event` for their own commit; only foreign subscribers do.
- `list_sessions` — `{id, op, filter?}`. Replies with `ack` carrying `[{session_id, metadata, ...summary}, ...]`. (TBD: shape of summary; default = `{session_id, metadata, created_at, last_seq}`.)
- `subscribe_session_list` — `{id, op, filter?}`. Replies with `ack` then a `session_list_snapshot`. Future deltas: pushed as `session_list_snapshot` events on every change (consistent with stage-2 design — no separate delta op).

Server → client frames (extends the `type` discriminator):

- `session_snapshot` — `{id, state, last_state_seq, last_state_ack_seq, slot_counter, events: [SessionEvent, ...]}`. Sent once after a `subscribe_session` ack. `events` is the suffix of events with `seq > last_state_ack_seq`.
- `session_event` — `{id, event}` where `event` is a `SessionEvent` (see below). Pushed for every event past the subscriber's latest delivery point. **Not** sent to the mutator for their own commits.
- `mutation_committed` — `{id, subscription_id, seq, update_id}`. The mutator-only response to a successful `mutate_session`. `id` is the mutate request's id (matches the standard request/response correlation); `subscription_id` and `update_id` are echoed back for client-side bookkeeping; `seq` is the seq of the resulting state-update event so the mutator knows their commit's position in the log.
- `session_list_snapshot` — `{id, sessions: [...]}`. Sent on subscribe and on every mutation that affects the list (creation/close/metadata — but metadata is immutable, so just create/close).

`SessionEvent` is itself a tagged union over `kind`:

- `state_update` — `{seq, ack_seq, state}`. `state` is the post-state. The `ack_seq` is the value the mutator carried.
- `load_result` — `{seq, slot, payload}`. `payload` is the capnweb-escaped representation of the entity.
- `load_error` — `{seq, slot, message}`.
- `invoke_result` — `{seq, slot, payload}`. `payload` is the capnweb-escaped result of the service call.
- `invoke_error` — `{seq, slot, message}`.
- `closed` — `{seq, reason}`. Last event ever emitted on the session.

### Mutation op shapes

The `ops` array in a `mutate_session` is a sequence of tagged objects. Each is parsed and validated up-front; unknown kinds reject the whole mutation.

- `track_entity {short_id}` — adds the entity to `session.tracked_entities` (via a new workstation registration owned by the session). Error if `short_id` does not resolve.
- `untrack_entity {short_id}` — removes the registration. Error if not currently tracked by this session.
- `track_service {short_id}` — analogous.
- `untrack_service {short_id}` — analogous.
- `load_entity {slot, short_id}` — schedule a load. Slot must be strictly greater than `slot_counter`; on commit, server bumps `slot_counter` to `slot`. The actual load runs after the mutation commits; result lands as a `load_result` / `load_error` event later.
- `invoke {slot, service_short_id, method_name, args}` — schedule a service-method invocation. `args` is capnweb-escaped JSON whose `["entity", ...]` / `["service", ...]` tags resolve against workstation-wide short IDs. Slot rule same as `load_entity`.
- `close` — close the session after this mutation commits. Must be the *only* op in `ops`; rejects mutations that mix `close` with any other op. After commit, the session is torn down and subscribers receive a `closed` event with the seq of the close.

### List-based escape format

Wire shape inspired by Cap'n'Web's escape rules. We do **not** call this "capnweb" anywhere in code — the name is "list-based escape" or just "wire escape".

Every literal JSON list `[1, 2, 3]` is encoded on the wire as `[[1, 2, 3]]`. A 2-element list whose first element is one of the reserved tag strings is a tagged special value:

- `["entity", "<short-id>"]` — entity reference.
- `["service", "<short-id>"]` — service reference.
- `["bytes", "<base64>"]` — raw bytes, standard base64.
- `["callback", "<slot>"]` — reserved for stage 5; rejected by the decoder until then.

Decoder semantics:
- Strings, numbers, bools, null pass through.
- Dicts are walked; values recursively decoded.
- Lists with length 2 whose element 0 is a reserved tag string: tagged special. Today all reserved tags are 2-element; future tag additions may be n-ary, in which case the codec adds the new tag's arity rule.
- Otherwise: literal-list escape. Must be a length-1 list whose element is itself a list; decode element 0 recursively. Any other shape is a validation error.

Module: `wire_escape.py`. Public API: `escape(value, *, encode_entity, encode_service)` / `unescape(value, *, lookup_entity, lookup_service)`. Pure functions; no registry mutations. The delivery helper passes callbacks that allocate short IDs and accumulate refs.

## Backend bookkeeping

### Session record

`WorkstationState.sessions: dict[UUID, SessionRecord]`. Each record carries (final shape after the 3c.2a refactor):

- `session_id: UUID`, `metadata: dict[str, Any]` (frozen at create), `created_at`.
- `state: dict[str, Any]` — current JSON state (after the latest accepted mutation).
- `event_seq: int` — high-water mark for any session event (state-update or result). Always advances; replaces the `last_state_seq + 1` arithmetic that 3b.1 used while result events didn't exist.
- `last_state_seq: int` — seq of the latest accepted *state-update* event. Used by snapshot replay's `last_state` field.
- `last_state_ack_seq: int` — `ack_seq` carried by the latest accepted mutation. Bounds the retention horizon — events with `seq < last_state_ack_seq` have been dropped from `event_log`.
- `event_log: list[SessionEvent]` — append-only, increasing-seq order with no gaps (across both state-updates and result events).
- `slot_counter: int` — high-water mark for client-allocated slot numbers.
- `last_mutator: (UUID, int) | None` — `(connection_id, subscription_id)` of the last accepted mutation, used for same-mutator skip-ahead.
- `tracked_services: set[ServiceKey]` / `tracked_entities: set[FxtrID]` — explicit pin sets (from `TrackXOp`). Used for double-track / not-tracked validation; *not* the source of truth for `tracking_sessions` membership.
- `entity_refcounts: dict[FxtrID, int]` / `service_refcounts: dict[ServiceKey, int]` — total refs the session holds for each ref. Sum of (1 if explicit pin) + (1 per retained event ref) + (1 per in-flight delivery contribution). The dict's domain is exactly the set of refs the session is keeping alive: `tracking_sessions` membership on the corresponding `TrackedXRecord` is in 1:1 correspondence with `refcount > 0`.
- `event_refs: dict[int, (set[FxtrID], set[ServiceKey])]` — for each retained result event in the log, the refs it materialized. Used to decrement refcounts when an event drops past the retention horizon.
- `closed: bool`.

**In-flight delivery refs are *not* on `SessionRecord`.** They live as task-local sets in the delivery helper. Session-close drains by walking `x_refcounts` directly, which already includes in-flight contributions, so it doesn't need to know about delivery state. (See "Invariants and primitives" below.)

**No lock.** Mutation processing is sync. Phase 1 / phase 3 of result delivery are sync. Phase 2 (bridge waits) is async but only touches `bridge_ready` / `bridge_error` — no registry mutations. Two operations arriving on different connections are serialized by the GIL within their sync portions.

**No `SlotRecord`.** Result events themselves serve the role: they sit in the event log keyed by their slot number, and `event_refs` carries the refs they materialized. Slot validation only needs `slot_counter`. In-flight delivery helpers carry their `(session_id, slot)` locally.

`SessionSubscription` carries `connection_id`, `subscription_id` (= the subscribe-request's id on that connection), and a reference to the connection's send queue.

### Invariants and primitives

The session-side resource model is governed by four invariants. The 3c machinery is built so each invariant is locally maintained by a small set of sync primitives — no global "is everything consistent?" sweep, no orphan-record cleanup branches.

**Invariants.**

- **(I) Registry liveness.** `TrackedXRecord ∈ state.X_records` ⟺ `registrations ∪ tracking_sessions ≠ ∅`. The actor that causes the last entry to drop runs `_maybe_release_X_record`.
- **(II) Reference-deliverability.** A result event in any `session.event_log` only carries short_ids whose record is in the registry *and* whose bridge (for services) is ready (`bridge_ready` set, `bridge_error is None`).
- **(III) Refcount = membership.** `session.session_id ∈ TrackedXRecord.tracking_sessions` ⟺ `session.x_refcounts[key] > 0`. The two sides only move together via the `_incref_session_X` / `_decref_session_X` primitives.
- **(IV) Refcount = sum of accounted holders.** `session.x_refcounts[key]` = (1 if explicit pin) + (1 per retained event ref) + (1 per in-flight delivery contribution). Session-close walks the dict directly — it doesn't need to peer into the categories.

**Sync primitives** (called between awaits; GIL-atomic):

- `_get_or_create_entity_record(state, fxtr_id) → TrackedEntityRecord` — get-or-allocate short ID, get-or-create record. Plumbing.
- `get_or_create_service_record(state, ref, source_scope) → TrackedServiceRecord` — get-or-allocate short ID, get-or-create record, **and on first creation spawn `service_record_worker(state, tracked, ref, source_scope)`** on `state.background_tg`. Sync (`start_soon` is sync). Returns the record without awaiting `bridge_ready`. (The async wait is a separate step in callers.)
- `_incref_session_entity(state, session, fxtr_id)` / `_incref_session_service(state, session, service_key)` — bump the session's refcount; on 0→1, add to `tracking_sessions` and notify the corresponding resource subscribers.
- `_decref_session_entity` / `_decref_session_service` — inverse. On 1→0, remove from `tracking_sessions`, call `_maybe_release_X_record`, notify.
- `_append_session_event(state, session, event, in_flight_e, in_flight_s)` — bump `event_seq`, append `event` to `event_log`, move `(in_flight_e, in_flight_s)` into `event_refs[event.seq]`, broadcast a `SessionEventFrame` to all session subscribers.

**Composite operations** (built from the primitives):

- *Explicit pin* (`TrackEntityOp`): validate against `tracked_entities`; if OK, add to `tracked_entities`, then `_get_or_create_entity_record` + `_incref_session_entity`. (Today's 3b.3 helpers `_add_session_entity_tracker` etc. become a thin wrapper around this composition.)
- *Explicit unpin* (`UntrackEntityOp`): validate; remove from `tracked_entities`, then `_decref_session_entity`.
- *Retention drop*: for each event past the horizon, walk `event_refs[seq]` and `_decref_session_X` for every ref, then `del event_refs[seq]`.
- *Session close*: walk `session.entity_refcounts` / `session.service_refcounts`. For each entry, the session is going away so the refcount is *unconditionally* dropping to 0; detach from `tracking_sessions` and `_maybe_release_X_record` for each. Clear all session bookkeeping. Drop from registry.
- *Result delivery* (load + invoke): the three-phase pipeline below.

The invariants compose: every registry mutation routes through one of these primitives; the primitives maintain (I) and (III) by construction; the delivery pipeline maintains (II) by waiting on bridges before commit; (IV) is a definitional accounting of where refcounts come from, drained correctly by session-close.

### Mutation processing

Sync; runs to completion without yielding to the event loop. Order:

1. **Stale-ack check.** Reject if `ack_seq < last_state_ack_seq` (must be monotonic non-decreasing). If `ack_seq < last_state_seq` and `last_mutator != (conn, sub)`, reject (someone else moved state forward). Same-mutator skip-ahead is allowed.
2. **Op validation (dry-run with working sets).** For each op, validate semantics against a planned-state copy (already-tracked / not-tracked / unknown short id / close-must-be-only-op / slot-must-be-strictly-greater). 3c adds: resolve invoke args' `["entity"|"service", short_id]` references; reject if any short id doesn't resolve. For load/invoke: each op's slot must be strictly greater than every slot claimed before it (whether in this mutation or in any earlier mutation). A single running counter (`max_slot_claimed`, initialized from the session's prior `slot_counter`) enforces both rules in one comparison.
3. **Apply patch.** JSON-Patch failures (incl. `test`) reject the mutation.
4. **Append `state_update` event.** Bump `event_seq`, `last_state_seq`, `last_state_ack_seq`, `last_mutator`. Append the event to `event_log`.
5. **Apply tracker ops (net diff).** `_add/remove_session_{entity,service}_tracker` for net adds/removes. *Done before retention prune* so explicit `track_*` ops in the same mutation that acks past a result event land before refcounts decrement — a mutation can pin a ref the result event held without it being released-and-re-added.
6. **Retention prune.** Drop events from the front of `event_log` whose `seq <= ack_seq`; for each dropped result event, decrement entity/service refcounts via `event_refs[seq]` and pop the entry. 1→0 transitions detach from the corresponding `tracking_sessions`.
7. **Apply slot ops.** Bump `slot_counter` to `max_slot_claimed`; spawn each `load_worker` / `invoke_worker` on `state.background_tg`.
8. **Broadcast** the new `state_update` event to foreign subscribers (suppress mutator). For close: terminal event has `closed=True`; tear down all session subscriptions; release all session pins (drain `tracked_entities`, `tracked_services`, and any remaining `event_refs`); drop the session from the registry; notify session-list subscribers.
9. **Mutator response.** Send `mutation_committed` correlated to the mutate request's id.

The originating mutator's subscription receives only `mutation_committed`; it is suppressed from the `state_update` broadcast. Result-event notifications (load/invoke results from earlier mutations) still flow through their stream — only the *self-state-update* is suppressed.

### Result delivery state machine

Both load and invoke deliveries walk the same state machine. Each non-terminal-to-terminal transition is either entirely sync or a single awaited primitive (`bridge_ready`):

```
   dispatched ──(async work fails before any attach)──→ error_pending
       │
       │ result computed; refs identified
       ▼
   attaching ──(session gone before/during phase 1)──→ aborted_silent
       │
       │ all attaches succeeded
       ▼
   waiting ──(bridge_error on any newly-created service)──→ rolling_back
       │
       │ all bridges ready
       ▼
   committing ──(session gone)──→ aborted_orphan
       │
       │ commit succeeds
       ▼
   delivered
```

Terminal states:

- **`delivered`** — event in log, in-flight refs promoted to `event_refs[seq]` by `_append_session_event`, subscribers notified. Source scope can drain.
- **`aborted_silent`** — session was already gone when phase 1 started, or went away mid-phase-1. Each `_incref_session_X` call sees a closed session and short-circuits; the helper rolls back what it already did via `_decref_session_X` (still under session-alive check). No event delivered.
- **`aborted_orphan`** — session went away during phase 2 or right before commit. Session-close already drained the refcount dict, including our in-flight contributions, so there's nothing to roll back. No event delivered.
- **`error_pending` (delivered as error)** — async dispatch / load failure before any attach. If session alive, deliver the error event with empty in-flight refs; else silent.
- **`rolling_back` (delivered as error)** — bridge failed mid-phase-2. If session alive, walk in-flight refs, `_decref_session_X` each, then deliver the error event. Else silent.

The async helper, sketched:

```
async def deliver_result_to_slot(state, session_id, slot, value, source_scope, *, kind):
    # `kind` selects load vs invoke event classes.
    # `value` is the loaded CBOR (load) or the FxtrServiceData result
    # (invoke), already in `source_scope`.
    in_flight_e: set[FxtrID] = set()
    in_flight_s: set[ServiceKey] = set()
    new_service_records: list[TrackedServiceRecord] = []

    # Phase 1 (sync): walk the value, attach each ref. Returns False
    # if the session was gone at any attach point — helper has rolled
    # back what it added so far.
    if not _phase1_attach_all(state, session_id, value, source_scope,
                              in_flight_e, in_flight_s, new_service_records):
        return  # aborted_silent

    # Phase 2 (async, no registry mutations).
    try:
        for tracked in new_service_records:
            await tracked.bridge_ready.wait()
            if tracked.bridge_error is not None:
                raise tracked.bridge_error
    except BaseException as exc:
        _phase_rollback_or_silent(state, session_id, slot, kind,
                                  in_flight_e, in_flight_s, error=str(exc))
        return  # rolling_back (delivered as error or silent)

    # Phase 3 (sync, atomic).
    _phase3_commit_or_orphan(state, session_id, slot, value, kind,
                             in_flight_e, in_flight_s)
```

`_phase1_attach_all`, `_phase_rollback_or_silent`, and `_phase3_commit_or_orphan` are sync helpers built on the primitives. The encoder (`escape` from `wire_escape.py`) runs inside `_phase3_commit_or_orphan`'s session-alive gate — it's pure (no registry mutations); the lookup callbacks just read short IDs that phase 1 already allocated.

Load and invoke share the helper; they differ only in:

- How they obtain `value` and `source_scope`. Load: `value = await state.entities_scope.load_cbor(fxtr_id)`; `source_scope = state.entities_scope`. Invoke: open a fresh `call_scope` (isolated, mountable, `include=[tracked_service.service_scope, state.entities_scope]`), dispatch through the tracked service, `value = await proxy.method(*args)`; `source_scope = call_scope`.
- The event class: `LoadResultEvent` / `LoadErrorEvent` vs. `InvokeResultEvent` / `InvokeErrorEvent`.
- Phase 2 footprint. Load CBOR can't carry `ServiceRef` leaves, so phase 2 is empty. Invoke results may surface new services; phase 2 awaits each.

When `deliver_result_to_slot` returns, the caller (the worker task) lets `source_scope` drain. For load, that's a no-op (the scope is `entities_scope` which is server-lifetime). For invoke, the `call_scope` opened by the worker drains on the `async with` exit; the workstation-side bridges into `service_scope` are "Independent" so they survive.

### Connection bookkeeping

`ClientConnectionRecord.subscriptions: dict[int, ConnectionSubscription]`, where `ConnectionSubscription = ResourceSubscription | SessionSubscription | SessionListSubscription` (tagged union, not nested per-kind dicts). All subscription ids share one space per connection.

## Substages

Stage 3 is broken into four substages so we can review at each step.

### 3a — Session lifecycle + listing (read-only sessions)

- Add `SessionRecord` skeleton (no slots, no async workers).
- Implement `create_session`, subscribe-session (snapshot only), unsubscribe-session, `list_sessions`, `subscribe_session_list`.
- No mutations yet — sessions are created with a fixed initial state (e.g. empty `{}`). Closing happens via a stage-3a-only `close_session` op (admin-only escape hatch, removed in 3b once `close` is a mutation op).
- Tests:
  - Create / subscribe / unsubscribe / list flow.
  - Two subscribers see the same snapshot.
  - Session-list subscription pushes new snapshots on create / close.
- Update CODE_OVERVIEW.md.

### 3b — Mutation core

Broken into three substages so each lands its own checkpoint.

#### 3b.1 — Patch + ack/error pipeline + retention horizon

- Implement `mutate_session` accepting an empty `ops` list, with JSON Patch only (RFC 6902 full dialect).
- Stale-ack semantics (must be monotonic non-decreasing; same-mutator skip-ahead allowed).
- Atomicity: failing patch rejects the whole mutation; no events appended.
- `mutation_committed` response to mutator; `state_update` broadcast (with self-suppression) to other subscribers.
- Event log + retention horizon (drop events with seq < last_state_ack_seq after each accepted mutation).
- Empty mutation (empty patch + no ops) is valid and just bumps the seq counter — useful for advancing ack_seq.
- Tests cover: stale-ack, same-mutator skip-ahead, no-op mutation, patch ops happy path (replace/add/remove/test/move/copy), event log retention, multi-subscriber broadcast + self-suppression.
- Update CODE_OVERVIEW.md.

#### 3b.2 — Close as a mutation op

- Implement `close` op inside the mutation `ops` array.
- `close` must be the *last* op; ops before it run normally so a
  mutation can release session state (Untrack, Unpreserve, patch,
  etc.) and then close in one atomic step. Anything after CloseOp
  is rejected.
- On commit: append a `closed` event with the mutation's seq; broadcast it to foreign subscribers; close ack to mutator (no `state_update`); release session registrations (none yet — wait for 3b.3 — but plumbing should be in place).
- Retire the stage-3a stand-in top-level `close_session` op (drop from protocol + handler + plan).
- Tests: close-mutation happy path, close-mixed-with-other-op rejection, foreign close event delivered while mutator gets only ack.
- Update CODE_OVERVIEW.md.

#### 3b.3 — Tracker ops (track/untrack entity, track/untrack service)

- Implement the four tracker ops. Resolve short IDs against the workstation registries; reject if unknown.
- Widen the `source` field on `Registration` / `EntityRegistration` from `RPCConnectionRecord` to `RPCConnectionRecord | SessionRecord`. Update `release_registration` / `release_entity_registration` to handle session sources (mainly: don't try to remove from a connection's registration list when the source is a session).
- Hook close to release all session registrations.
- Tests:
  - Track entity registered by an RPC connection; drop the connection; assert session keeps the entity alive (registry still has a session-sourced registration).
  - Untrack returns the session to "no claim" state.
  - Mixed tracker ops in one mutation; atomicity on failure.
  - Close releases all trackers.
- Update CODE_OVERVIEW.md.

### 3c — Slot machinery (load + invoke)

Broken into six substages so each lands its own checkpoint. The order matters: 3c.2a refactors the data model and primitives, 3c.2b lights up `load_entity` on top of them, 3c.3a refactors `register_service` to share the service-record creation primitive, 3c.3b lights up `invoke`, 3c.4 is cross-cutting tests.

#### 3c.1 — List-based escape codec (`wire_escape.py`)

Pure functions, no dependency on workstation state.

- `escape(value, *, encode_entity, encode_service) → JSONLike`. Walks a Python value (typically the result of a CBOR decode); wraps every literal list as a single-element list; replaces `FxtrID`s (via `encode_entity`) and `ServiceRef`s (via `encode_service`) with `["entity", short_id]` / `["service", short_id]` tags. Dicts are walked recursively. Strings/numbers/bools/null pass through.
- `unescape(value, *, lookup_entity, lookup_service) → Any`. Inverse: tagged forms become `FxtrID`s / `ServiceRef`s (via the provided lookup); single-element list of list = literal-list escape; dicts walk recursively. Validation errors raise `ValueError`. Unknown tag strings (e.g. `["callback", N]` until stage 5) reject.
- Reserved tag set: `{"entity", "service", "bytes", "callback"}`. Currently all 2-element; future n-ary additions extend the codec's per-tag arity table. (`bytes` payload is standard base64; the codec encodes / decodes mechanically — no callback needed.)
- Module docstring: one-line credit to Cap'n'Web for the escape idea; otherwise no "capnweb" references in the codebase.
- Tests in `tests/test_wire_escape.py`: round-trips for all primitives, lists (incl. nested + empty), dicts, tagged values, mixed; rejection paths; reject-callback sentinel; rejection for unknown tags or wrong arity.
- Update CODE_OVERVIEW.md.

#### 3c.2a — Refcount + event-counter + primitives refactor

Data-model and bookkeeping refactor, no new wire ops, no new behavior. Existing 3a/3b/3d tests must continue to pass.

- Add `event_seq: int` to `SessionRecord`. `apply_mutation` now allocates `new_seq = record.event_seq + 1` and bumps both `event_seq` and `last_state_seq`. (Today the two are coincident; introducing the split now means 3c.2b can bump `event_seq` without touching `last_state_seq`.)
- Add `entity_refcounts: dict[FxtrID, int]` / `service_refcounts: dict[ServiceKey, int]` / `event_refs: dict[int, (set[FxtrID], set[ServiceKey])]` to `SessionRecord`.
- Introduce sync primitives:
  - `_incref_session_entity(state, session, fxtr_id)` / `_incref_session_service(state, session, service_key)` — bump refcount; on 0→1 add to `tracking_sessions`, notify.
  - `_decref_session_entity` / `_decref_session_service` — inverse; on 1→0 remove and `_maybe_release_X_record`, notify.
- Refactor existing 3b.3 helpers (`_add_session_entity_tracker` etc.) to compose the new primitives: pin set update + `_incref_session_X`. Composite untrack: pin set update + `_decref_session_X`.
- Refactor session close to walk `entity_refcounts` / `service_refcounts` and call the per-key detach side-effects (no decrement; we're zeroing out). Result must be identical to today's behavior since pre-3c the only contributions are explicit pins.
- Tests: existing test suite continues to pass. Add a focused unit test for the primitives: pin entity → refcount 1 → unpin → refcount 0; double-attach via two paths sums correctly; session close drains a session with mixed pin / event-ref contributions (synthesize a fake event_refs entry to exercise the multi-source case).
- Update CODE_OVERVIEW.md.

#### 3c.2b — `LoadEntityOp` + delivery worker

- Add `LoadEntityOp(slot: int, short_id: str)` to the `MutationOp` union. Add `LoadResultEvent(seq, slot, payload)` and `LoadErrorEvent(seq, slot, message)` to the `SessionEvent` union.
- Mutation op validation: short_id resolves; slot strictly greater than every slot claimed before (running `max_slot_claimed` initialized from `slot_counter`, bumped per op); apply phase sets `slot_counter = max_slot_claimed`.
- Apply phase spawns `state.background_tg.start_soon(load_worker, state, session_id, slot, fxtr_id)`.
- `load_worker` calls `await state.entities_scope.load_cbor(fxtr_id)` to get the CBOR dict, then runs the `deliver_result_to_slot` helper with `kind="load"`, `value=cbor`, `source_scope=state.entities_scope`. Phase 2 is empty (CBOR has no `ServiceRef` leaves).
- `_phase1_attach_all`: walk `value`, identify `FxtrID` leaves; for each, `_get_or_create_entity_record` + `_incref_session_entity` under session-alive gate. Returns False on session-gone (helper has rolled back).
- `_phase3_commit_or_orphan`: under session-alive gate, run `escape(value, ...)` with lookup callbacks that read the already-allocated short IDs; build `LoadResultEvent`; `_append_session_event`. On session-gone, no-op (orphan path; nothing to roll back since session-close drained).
- `_phase_rollback_or_silent` (used here only for unexpected exceptions in phase 1's encode walk): walk in-flight, detach, deliver `LoadErrorEvent`.
- Tests: load primitive entity; load entity with children (short_ids appear in payload, refcounts bump on session, children become workstation-tracked); slot monotonicity violation rejects; load of unknown short_id rejects at op-validation time; close-during-load no-ops cleanly; load_cbor failure path delivers `LoadErrorEvent`.
- Update CODE_OVERVIEW.md.

#### 3c.3a — Extract `get_or_create_service_record`

Refactor only, no new wire ops, no behavior change. Existing service registration tests must continue to pass.

- Pull the get-or-create + first-time-worker-spawn logic out of `WorkstationServiceImpl.register_service` into a sync helper `get_or_create_service_record(state, ref, source_scope) → TrackedServiceRecord` in `state.py`. Returns the record without awaiting `bridge_ready`.
- `register_service` now: `tracked = get_or_create_service_record(state, service, call_scope)` → `await tracked.bridge_ready.wait()` → `bridge_error` check → sync name-conflict + Registration + mount + end-callback.
- Existing rollback path stays — it already handles the case where `tracked` was created but no Registration was attached.
- Tests: pre-existing service-registration suite passes.
- Update CODE_OVERVIEW.md.

#### 3c.3b — `InvokeOp` + delivery worker

- Add `InvokeOp(slot: int, service_short_id: str, method_name: str, args: list[JSON])` (args is the list-escaped JSON). Add `InvokeResultEvent(seq, slot, payload)` and `InvokeErrorEvent(seq, slot, message)`.
- Mutation op validation: slot rules same as load; service short_id resolves; `unescape(args, ...)` succeeds (every `["entity", x]` / `["service", x]` resolves in the workstation registries; `["callback", x]` rejects until stage 5).
- Apply phase: spawn `state.background_tg.start_soon(invoke_worker, state, session_id, slot, service_key, method_name, resolved_args)`.
- `invoke_worker` opens an isolated mountable `call_scope` with `include=[tracked.service_scope, state.entities_scope]`. Resolves args (lift `FxtrID`s / `ServiceRef`s into `call_scope`), dispatches via `UnknownServiceProxy` against the tracked service, awaits the result. Calls `deliver_result_to_slot` with `kind="invoke"`, `value=result`, `source_scope=call_scope`. Phase 2 awaits `bridge_ready` on each newly-created service record.
- `_phase1_attach_all` for invoke: walks `FxtrID` and `ServiceRef` leaves in the result. For services it uses `get_or_create_service_record` (introduced in 3c.3a) — sync, may spawn worker — and `_incref_session_service`. Newly-created records are appended to `new_service_records` so phase 2 knows what to wait on.
- `_phase_rollback_or_silent` for invoke: bridge error from phase 2 walks in-flight refs and detaches each (sync, under session-alive gate); delivers `InvokeErrorEvent`.
- Dispatch errors before phase 1 (exception from the proxy call): `error_pending` path — deliver `InvokeErrorEvent` if session alive, else silent.
- Tests:
  - Invoke a service method tracked by another session.
  - Invoke result that references an entity → that entity becomes workstation-tracked (with refcount on the calling session).
  - Invoke result that returns a `ServiceRef` → new `TrackedServiceRecord`, worker spawned, refcount on calling session, future `get_service` / invoke against the new short_id works.
  - Close-during-invoke discards result cleanly (`aborted_orphan`); no leaks; bridges that completed during phase 2 land in records that are released by session-close drain.
  - Bridge error during phase 2 → `InvokeErrorEvent` and full rollback.
  - Op-validation rejects bad service short_id, bad arg refs, `["callback", x]`.
- Update CODE_OVERVIEW.md.

#### 3c.4 — Cross-cutting refcount/retention edge-case tests

The primitives' invariants get end-to-end exercise here.

- Tests:
  - Load result references a child entity → child stays alive while the event is retained; once the event drops past `last_state_ack_seq`, the child's session refcount decrements; if no other holders, the entity is released and `tracking_sessions` reflects this.
  - Same scenario but the session pins the child via `track_entity` in the mutation that acks → child stays alive (refcount remains ≥ 1 from the explicit pin).
  - Child kept alive even when the source RPC connection drops (analogous to 3b.3's `test_session_keeps_entity_alive_after_rpc_drops` but child sourced via load).
  - Close-during-in-flight (load): slow load worker, close the session before it lands; deliver path no-ops cleanly; no leaks.
  - Close-during-in-flight (invoke + new service): slow bridge, close the session while phase 2 is still awaiting; the orphan-record path doesn't trigger because session-close drains the in-flight refcount contribution; bridge eventually completes and the record releases via the post-bridge maybe-release.
- Update CODE_OVERVIEW.md.

### 3d — Cross-resource visibility polish

- Extend the stage-2 entity / service resource snapshots with `pinned_by_sessions: [uuid, ...]`.
- Push fresh snapshots on session-tracker changes.
- Tests:
  - Tracking from a session updates the corresponding entity/service record's `pinned_by_sessions`.
  - Untracking / closing the session removes the entry.
- Update CODE_OVERVIEW.md.

## Tests (cross-cutting)

- Spec-driven tests for the mutation ACID semantics: stale-ack rejection, same-client overwrite allowance, slot-monotonicity, event-log replay on subscribe.
- Multi-client tests: two clients on the same session, ensure only one mutation wins per state version.
- Cross-session tests: tracking an entity registered by an RPC connection, then dropping that RPC connection, asserts the session keeps the entity alive.
- Retention-horizon test: verify that events older than `ack_seq_of_last_state_update` are dropped from the log; new subscribers receive only the suffix.
- Close-during-in-flight: schedule a slow load, close the session before it completes, ensure the deliver task discards the result without crashing the workstation.

## Implementation steps

- [x] **3a.** `SessionRecord` skeleton, session lifecycle ops, list + list-subscribe.
- [x] Update CODE_OVERVIEW.md (after 3a).
- [x] **3b.1.** Mutation pipeline: JSON Patch + ack/error + retention horizon (no ops).
- [x] Update CODE_OVERVIEW.md (after 3b.1).
- [x] **3b.2.** Close as a mutation op; retire stand-in `close_session`.
- [x] Update CODE_OVERVIEW.md (after 3b.2).
- [x] **3b.3.** Tracker ops + session-sourced registrations.
- [x] Update CODE_OVERVIEW.md (after 3b.3).
- [x] **3d.** Extend stage-2 snapshots with `pinned_by_sessions`.
- [x] Update CODE_OVERVIEW.md (after 3d).
- [x] **3c.1.** List-based escape codec in `wire_escape.py`.
- [x] Update CODE_OVERVIEW.md (after 3c.1).
- [x] **3c.2a.** Refcount + event-counter + primitives refactor.
- [x] Update CODE_OVERVIEW.md (after 3c.2a).
- [x] **3c.2b.** `LoadEntityOp` + delivery worker.
- [x] Update CODE_OVERVIEW.md (after 3c.2b).
- [x] **3c.3a.** Extract `get_or_create_service_record` from `register_service`.
- [x] Update CODE_OVERVIEW.md (after 3c.3a).
- [x] **3c.3b.** `InvokeOp` + delivery worker.
- [x] Update CODE_OVERVIEW.md (after 3c.3b).
- [x] **3c.4.** Refcount/retention edge-case tests.
- [x] Update CODE_OVERVIEW.md (final).

## Open questions to revisit

- Memory-pressure policy if the event-log retention horizon never advances (e.g. a session that nobody mutates but has a steady stream of in-flight results piling up). Phase-0 acceptable to defer.
- Should the invoke worker open `call_scope` per-invocation (current plan) or hold one open for the duration of a "method invocation batch"? Per-invocation is simpler and matches `register_service`'s call-scope discipline; revisit only if it shows up as a hotspot.

## Current status (running notes)

Substages 3a, 3b.1, 3b.2, 3b.3, and 3d are landed. Stage 3 is feature-complete except for the 3c slot machinery (`load_entity` / `invoke` / list-based escape codec / refcount-driven retention bookkeeping).

**What exists today:**

- `WorkstationState.sessions` registry with `SessionRecord` carrying state + event log + tracker sets.
- `/client` ops: `create_session`, `subscribe_session`, `list_sessions`, `subscribe_session_list`, `unsubscribe`, `mutate_session`. `mutate_session.ops` accepts `CloseOp`, `TrackEntityOp`, `UntrackEntityOp`, `TrackServiceOp`, `UntrackServiceOp`.
- Mutation pipeline: stale-ack semantics, RFC 6902 patch, atomicity, retention horizon, self-vs-foreign event delivery, terminal-close via `closed=true` on `StateUpdateEvent`.
- Session trackers split from RPC registrations: `Registration` is RPC-only; `TrackedXRecord` carries `tracking_sessions: set[UUID]`; `SessionRecord` carries `tracked_services` / `tracked_entities` sets. Cleanup helpers `_maybe_release_{service,entity}_record` shared between RPC release and session untrack paths.
- Stage-2 entity / service snapshots include `pinned_by_sessions: [uuid_string, ...]`, kept live.
- 128 tests passing across 3a / 3b.1 / 3b.2 / 3b.3 / 3d (and pre-existing 1/2 tests).

**Architectural notes carried forward:**

- *Sessions hold their registering RPC connections alive* on graceful drain (services only — entities are copied). Cancellation/crash bypasses the wait. Documented in the resolved design list above.
- No `WorkstationState.lock`; no per-session lock. All registry mutations are sync; GIL atomicity covers it.
- `MutationCommittedResponse` distinct from `state_update` events (mutator-only); foreign subscribers get `state_update`; foreign-broadcast precedes the response on the wire.

**3c-specific design decisions (settled, codified above):**

- Invariants-first framing. (I) Registry liveness, (II) reference-deliverability, (III) refcount = membership, (IV) refcount = sum of accounted holders. The 3c primitives (`_incref_session_X`, `_decref_session_X`, `_get_or_create_X_record`, `_append_session_event`) maintain (I)/(III) by construction; the three-phase delivery pipeline maintains (II) by waiting on bridges before commit.
- Three-phase delivery (sync attach / async wait / sync commit) is the only path that introduces session-side refs. No orphan-record cleanup branch — phase-1 attaches under a session-alive gate, so a record never exists in the registry without at least one holder.
- Drop `SlotRecord` entirely. Result events themselves carry the slot info; per-session `event_refs[seq]` sidecar tracks the entities/services each retained result event references. In-flight delivery refs are *not* on `SessionRecord` — they live as task-local state in the helper, and session-close drains via `x_refcounts` (which already accounts for them).
- Slot allocation is client-driven and validated at op time: each slot must be strictly greater than every slot claimed before it (in this mutation or any earlier mutation). A single running `max_slot_claimed` counter enforces both rules in one comparison.
- Invoke dispatch uses the `UnknownService` machinery; args/results are list-escape-encoded and lifted to `FxtrServiceData` at dispatch time.
- `get_or_create_service_record` is the shared sync primitive for "make this `ServiceRef` workstation-tracked"; `register_service` and the invoke delivery worker both use it. The async `bridge_ready.wait()` is a separate step in each caller (gating its own ready-to-commit path).
- Callbacks (`["callback", N]`) deferred to stage 5; codec rejects them.
- "List-based escape" naming throughout the code; one-line Cap'n'Web credit only at the top of `wire_escape.py`.
