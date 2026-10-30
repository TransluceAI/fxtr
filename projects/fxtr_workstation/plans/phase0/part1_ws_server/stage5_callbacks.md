# Stage 5 — Callbacks

Goal: add the callback primitive described in `../sketch.md`. The
workstation mints small workstation-owned services that expose a
typed `EventListenerProto`; each `notify` invocation routes into the
owning session's event log against a slot. Lifetime piggy-backs on
the existing service refcount machinery, with one additional
contribution category for "open callback".

The async-mutation foundation that this stage rides on is
described in
[`stage5_extra1_async_mutation.md`](./stage5_extra1_async_mutation.md);
the relevant ingredients here are the four-phase
`apply_mutation` pipeline (validate → async pre-commit → validate
invoke args → commit), per-session mutation queues, and
`MutationRejectedResponse` with typed reasons. 5a/5b/5c landed
*before* that refactor and were materially reshaped during it; the
5b/5c sections below describe the post-refactor state, not the
landed-then-rewritten one.

## Conceptual model

A callback has a **send side** and a **receive side**, decoupled:

- The **send side** is a regular workstation-tracked service (it has
  a `TrackedServiceRecord`, a `ServiceKey`, an `s{N}` short_id, and
  participates in the standard service-refcount machinery). Other
  RPC peers can hold bridges to it; sessions can `track_service` it
  via the explicit-pin path; it shows up in resource snapshots like
  any other service.
- The **receive side** is the slot inside the owning session that
  `notify` invocations land on. It's controlled by
  `create_callback` (open) and `close_callback` (close), and exists
  only on the originating session.

These are independent: closing the receive side does *not* drop the
send-side ref if other things are holding it (sessions, RPC peers).
A session is allowed to `track_service` the send side independently
of having the receive side open. The two sides communicate via the
impl's `_closed` flag — set when the receive side closes, observed
by `notify` / `listening` so external callers learn the listener is
gone via the bool return.

## Resolved design decisions

- **`["callback", slot]` is a client-side convenience tag.** It is
  legal *only on the inbound mutation-arg path* (specifically
  `InvokeOp.args`), where `unescape` resolves it to a real
  `ServiceRef` built off the bound `CallbackRecord.service_key`.
  The outbound (server → client) encoder always uses
  `["service", short_id]` for callback refs, like any other
  service. Cross-session args (a session passing its own callback
  to another session's invoke arg) pass via `["callback", slot]`
  too — the lookup is rooted in the *mutating* session's
  `open_callbacks`.
- **A new "open callback" refcount category.** The session's
  `service_refcounts[callback_service_key]` collapses contributions
  from (1 if explicit pin) + (1 per retained event ref) + (1 per
  in-flight delivery contribution) + (1 if open callback). All four
  categories share the existing `_incref_session_service` /
  `_decref_session_service` primitives; the open-callback hold is
  a documentation distinction (which call site bumps it) rather
  than a code-level one.
- **`close_callback` is the *only* way to close the receive side.**
  `untrack_service` decrements the explicit-pin contribution
  (third-party send-side track), not the open-callback contribution.
  Op-validation rejects `close_callback` for a slot that isn't
  currently open; rejects `untrack_service` if the session never
  pinned the service (existing rule).
- **Callbacks are fully built before the mutation commits.**
  `CreateCallbackOp` is processed in `apply_mutation`'s async
  pre-commit phase: each requested slot has a
  `callback_record_worker` started via
  `task_group.start(...)` which mounts the impl, brings up the
  underlying `TrackedServiceRecord` (real
  IRHandle-derived `ServiceKey`), awaits `bridge_ready`, and then
  signals readiness with the fully-populated `CallbackRecord`. By
  the time the mutation commits, every requested callback is real
  and resolvable. There is no "pending" intermediate state.
- **Bridge failure rejects the whole mutation.** A workstation-
  internal bridge failing is a "we've broken an invariant"
  condition, not a user-facing error mode. The pre-commit phase
  releases any callbacks already built and rejects the mutation as
  `MutationRejectedResponse(reason="callback_setup_failed")`.
  There is no `CallbackErrorEvent` on the wire.
- **No net-diffing for callback ops.** `CreateCallbackOp` and
  `CloseCallbackOp` are tracked as explicit
  `callback_create_slots` / `callback_close_slots` lists in op
  order. A within-mutation `[Create N, Invoke uses N, Close N]`
  sequence really creates the callback, the invoke uses it, and
  the close runs after the invoke worker has claimed its in-flight
  delivery contribution. Slot monotonicity prevents re-creating a
  closed slot, so the explicit-list approach is unambiguous.
- **`impl_scope` follows the `register_service` shape.** A
  per-callback `impl_scope` is opened by the
  `callback_record_worker`, the impl is mounted on it, and the
  standard `get_or_create_service_record` flow brings up a
  service_scope and sets `bridge_ready`. When service_scope drains,
  the bridge drops, and `impl_scope` completes its drain. The
  worker holds `impl_scope` open via `async with` until shutdown,
  mirroring how `register_service`'s `call_scope` stays pinned by
  its bridge until the registration releases.
- **`notify` failures bubble to the RPC caller, not the session.**
  Phase-1 attach failures or phase-3 encode failures raise out of
  the impl method. The session's event log only sees a
  `CallbackEvent` for successful deliveries.
- **`listening()` is a pure read of `_closed`.** No session-side
  bookkeeping; doesn't touch the event log; doesn't decref anything.

## Wire-protocol changes

### Mutation ops (landed in 5b)

- `create_callback {slot}` — allocate the receive side at `slot`.
  Slot validation follows the existing strict-monotonicity rule
  (running `max_slot_claimed`).
- `close_callback {slot}` — close the receive side. Validation:
  `slot` must currently be open at that point in the mutation's
  op walk (in `record.open_callbacks` or being created earlier in
  this mutation). Mixing `close_callback` with other ops in the
  same mutation is allowed (unlike top-level `close`).

### Session events

5d adds `CallbackEvent {seq, slot, payload}` to the
`SessionEvent` tagged union — one per successful `notify`
invocation. `payload` is the list-escaped form of the
`FxtrServiceData` argument; refs in the payload are resolved
through the same allocator/incref machinery as invoke results.

There is no `CallbackReadyEvent` (callbacks are ready by the time
the mutation's `StateUpdateEvent` commits) and no
`CallbackErrorEvent` (bridge failures reject the mutation
synchronously).

### Codec (landed in 5c)

`unescape` accepts an optional
`lookup_callback: Callable[[int], ServiceRef[Any]] | None`. The
`["callback", slot]` tag resolves through it; without one the tag
is rejected. The lookup is supplied by `apply_mutation`'s phase-3
invoke-arg validation, after pre-commit has built every callback
the mutation requested. The lookup looks up the slot against
`pending_creates` ∪ `record.open_callbacks` and returns a
`ServiceRef` constructed from the bound
`CallbackRecord.service_key`.

The encoder remains 2-callback (`encode_entity` / `encode_service`)
— there's no outbound use of the callback tag.

## Backend bookkeeping (landed in 5b, reshaped in extra1)

### `SessionRecord.open_callbacks`

`dict[int, CallbackRecord]` — receive sides currently open in
this session. Populated by `apply_mutation`'s commit phase
attaching pre-built records; removed by `close_callback` and the
session-close sweep.

### `CallbackRecord`

A small dataclass; every field is required and populated at
construction:

- `slot: int`
- `impl: WorkstationCallbackImpl`
- `tracked: TrackedServiceRecord`
- `service_key: ServiceKey`
- `service_short_id: str`

The `callback_record_worker` builds the record before signalling
readiness via `task_status.started(record)`. There is no
"pending" lifecycle marker — a `CallbackRecord` either exists in
`session.open_callbacks` fully formed, or doesn't exist at all.

### `WorkstationCallbackImpl`

`TypedServiceImpl[EventListenerProto]` carrying `(state,
session_id, slot, _closed: bool)`. Methods:

- `notify(payload)`: if `_closed`, return False immediately. Else
  run the 3-phase delivery pipeline against
  `(session_id, slot, payload, source_scope=dispatch_scope)`.
  Phase 3 builds and appends a `CallbackEvent`. On phase-1 abort
  (session gone), return False. On phase-3 commit (success),
  return True. Phase-1/3 exceptions propagate to the caller as RPC
  errors.
- `listening()`: return `not self._closed`.

Today (5a/5b/5c landed): the impl exists, `listening()` works,
and `notify` short-circuits to False when `_closed`. The
non-closed branch raises `NotImplementedError` — wiring it is the
5d task below.

### Refcount fourth category

Open-callback contributions ride the existing
`_incref_session_service` / `_decref_session_service` primitives —
the dict (`service_refcounts`), the membership-maintaining tail,
and `_maybe_release_service_record` are all shared.
"Open-callback hold" is a documentation distinction at the call
site (which mutation/sweep path is bumping the count), not a
code-level one. A session can hold multiple kinds of contribution
on the same key simultaneously (e.g. `track_service` of its own
callback's send side), and any non-zero total keeps the session
in `tracking_sessions`. The `pinned_by_sessions` snapshot field
continues to reflect "tracking_sessions membership" so any
contribution kind makes a session "pin" the service.

### `callback_record_worker`

Spawned by `apply_mutation`'s async pre-commit phase via `await
self.background_tg.start(callback_record_worker, ...)`. Sketch:

```
async def callback_record_worker(state, session_id, slot, *, task_status):
    async with FxtrScope(parent=state.root_scope, isolated=True, mountable=True) as impl_scope:
        impl = WorkstationCallbackImpl(state, session_id, slot)
        impl_ref = mount(impl, impl_scope)
        with state.mutation_context():
            tracked = get_or_create_service_record(state, impl_ref, impl_scope)
        await tracked.bridge_ready.wait()
        if tracked.bridge_error is not None:
            with state.mutation_context():
                maybe_release_service_record(state, tracked)
            raise tracked.bridge_error
        callback_record = CallbackRecord(
            slot=slot, impl=impl, tracked=tracked,
            service_key=tracked.service_key,
            service_short_id=tracked.short_id,
        )
        task_status.started(callback_record)
        await tracked.shutdown_event.wait()
    # impl_scope drains here.
```

`task_group.start` raises any exception the worker raises before
calling `task_status.started`; pre-commit catches that and rolls
back any sibling pre-built callbacks (release each tracked record
via `maybe_release_service_record`) before rejecting the mutation.

### Session close sweep

`_close_session_drain_trackers` walks `session.open_callbacks`
and pops each entry, flipping `cb_record.impl._closed = True`.
Refcount cleanup is handled by the subsequent
`service_refcounts` drain — the open-callback contribution lives
in the same dict as every other category. There's no per-state
dispatch (every `CallbackRecord` is fully formed by virtue of
having made it into `open_callbacks`).

### `apply_mutation` integration

The four-phase pipeline (see
`stage5_extra1_async_mutation.md`) handles callback ops as
follows:

- **Phase 1 (validate ops, sync).** `CreateCallbackOp`
  participates in the running `max_slot_claimed` check;
  `CloseCallbackOp` validates `slot ∈ working_callback_slots` (a
  set tracking which slots are open at that point in the op
  walk). The slots are appended to `callback_create_slots` /
  `callback_close_slots` in op order — no net-diffing.
- **Phase 2 (async pre-commit).** For each slot in
  `callback_create_slots`: `await background_tg.start(
  callback_record_worker, …)`; collect into `pending_creates:
  dict[int, CallbackRecord]`. On any worker exception: roll back
  the already-built records via `maybe_release_service_record`
  and reject as `callback_setup_failed`.
- **Phase 3 (validate invoke args, sync).** Walk each invoke
  spec's args via `unescape` with a real
  `ServiceRef`-returning `lookup_callback` (looking up against
  `pending_creates` ∪ `record.open_callbacks`). On failure:
  roll back pre-commit creates; reject as `validation_failed`.
- **Phase 4 (commit, sync, in `mutation_context`).** Apply
  patch → tracker adds → attach pre-built callbacks to
  `record.open_callbacks` (incref open-callback contribution
  per attach) → claim invoke / load arg refs (in-flight delivery
  contributions) and spawn workers → tracker removes (incl.
  `close_callback`: pop from `open_callbacks`, set
  `cb.impl._closed = True`, decref open-callback contribution)
  → retention prune → close drain (if applicable) → build /
  append / broadcast `StateUpdateEvent` → mutator response →
  drop the session if closing.

Note the close ordering: `close_callback`s run *after* invoke
arg-claiming, so a within-mutation `[Create N, Invoke uses N,
Close N]` keeps the underlying record alive across the close
via the invoke's in-flight delivery contribution.

## `notify` 3-phase delivery (5d, the work this stage still
needs)

`notify` runs entirely on the *receive* side — the workstation's
own impl is the dispatch target, so there are no callback-shaped
args in flight. The payload is regular `FxtrServiceData`. The
3-phase pipeline mirrors the existing invoke result delivery:

- **Phase 1 (sync, under session-alive gate).** Walk the payload
  for `FxtrID` and `ServiceRef` leaves; for each:
  - FxtrID: `_get_or_create_entity_record` (record minted under
    a session-alive check); `_incref_session_entity`. Add to
    `in_flight_e`.
  - ServiceRef: `get_or_create_service_record` (mints a record +
    spawns its `service_record_worker` on first creation);
    `_incref_session_service`. Add to `in_flight_s` and (if
    newly created) to `new_service_records`.
  Returns False if the session is gone — no event to deliver,
  in_flight sets are empty (the session-alive check short-
  circuited).
- **Phase 2 (async).** For each newly-created service record,
  await `bridge_ready`. For each entity FxtrID that wasn't
  already in `entities_scope`, `await
  state.entities_scope.copy_from_scope(fid, source_scope)`.
  Errors raise out and unwind via task-group cancellation; the
  rolling_back path runs phase-1 decrefs and re-raises so the
  RPC caller sees the error.
- **Phase 3 (sync).** Re-check session-alive. Encode the
  payload via `wire_escape.escape` (replacing refs with their
  workstation short_ids). Build a `CallbackEvent`; bump
  `event_seq`; `_append_session_event(...)` (which appends to
  the event log, records `event_refs[seq] = (in_flight_e,
  in_flight_s)` so retention bookkeeping decref's them when the
  event drops past the horizon, and broadcasts a
  `SessionEventFrame` to subscribers). Return True.

The shape is identical to `deliver_invoke_result_to_slot` except
for the event class. Plan:

1. Either factor out a shared `_deliver_result_to_slot(state,
   session_id, slot, payload, source_scope, *, build_event:
   Callable[[int], SessionEvent])` helper, or write a parallel
   `deliver_notify_result_to_slot` that mirrors the invoke
   helper. Lean shared.
2. Wire `WorkstationCallbackImpl.notify`:
   - if `_closed`: return False.
   - else: capture the dispatch `source_scope`; call the
     delivery helper; return True on commit, False on
     aborted_silent / aborted_orphan, raise on
     rolling_back.

### Bool-return semantics

- `True` — payload was delivered to the session's event log.
- `False` — the receive side is closed (either `_closed` was
  already set, or the session went away mid-delivery). Caller
  should stop sending.
- Exception — encode failure or other phase-1/3 error. Bubbles
  to the RPC caller as a method-call error. Not surfaced in the
  session's event log.

## Tests (current state + 5d additions)

The 5b/5c landed tests cover the lifecycle (create / close,
session close, slot monotonicity) without `notify` integration.
5d adds:

- **Round-trip happy path.** Session creates callback at slot N;
  RPC peer (registered as a service) receives the callback ref
  via an invoke arg; peer calls `notify` with a primitive
  payload; session sees a `CallbackEvent`; `notify` returned
  True. `listening()` returns True.
- **Notify with refs in payload.** Payload includes a `FxtrID`
  / `ServiceRef`; phase-1 attaches; refcounts bump; event
  payload includes the short_ids; refcount-retention on the
  result event drops the contributions past the horizon as
  expected.
- **Cross-session passing.** Session A creates a callback;
  session A invokes a service tracked in session B with
  `["callback", slot]` as an arg; session B's invoke result
  encodes the ref as `["service", short_id]`; session B's
  invoked code calls `notify` against it; session A sees a
  `CallbackEvent`.
- **Close from session.** Session emits `close_callback` op;
  subsequent `notify` from the peer returns False;
  `listening()` returns False; the underlying service_scope
  drains (no other holders).
- **Drained-but-alive.** Session closes the callback while the
  peer still holds a bridge; service_scope stays alive
  (drained-but-alive surfaced in the snapshot); peer's `notify`
  / `listening` calls return False; eventually peer drops the
  bridge and the scope fully drains.
- **Notify error bubbles.** Force a phase-3 encode error (e.g.
  unencodable value); `notify` raises; no event in session log;
  no refcount changes survive.
- **Within-mutation create+invoke+close.** A single mutation
  `[CreateCallbackOp(N), InvokeOp(uses ["callback", N]),
  CloseCallbackOp(N)]` succeeds: pre-commit creates N; phase-3
  decode resolves to a real ServiceRef; commit attaches +
  spawns invoke worker (claiming an in-flight contribution) +
  closes N (decrefs open-callback contribution); the invoke
  result lands later and the worker eventually drains.

Tests already landed in 5a/5b/5c remain valid — the lifecycle
shape didn't change, the assertions about `state` / `ready`
fields were never checked end-to-end.

## Debug UI

- Render `CallbackEvent` rows in the session event log (5e).
  `CallbackEvent` rows are collapsible (payload is potentially
  large).
- Extend the session debug snapshot to include
  `open_callbacks: {slot → service_short_id}` so the per-session
  panel shows live receive-side state.
- The existing services table already surfaces "drained-but-
  alive" — callbacks ride that without changes.

## Substages

### 5a — `EventListenerProto` + `WorkstationCallbackImpl` skeleton (LANDED)

`EventListenerProto` lives in `protocols.py`;
`WorkstationCallbackImpl` lives in `callback_impl.py`. The impl
holds `_closed` and routes `listening()` / `notify()` accordingly;
`notify` raises `NotImplementedError` for the non-closed path
(the 3-phase delivery wires in 5d).

### 5b — `CreateCallbackOp` / `CloseCallbackOp` + lifecycle (LANDED, reshaped in extra1)

`CallbackRecord` lives in `state.py` (required fields only — no
`state` / `ready`). `callback_record_worker` opens an
`impl_scope`, mounts the impl, brings up the underlying tracked
record, and signals readiness via `task_status.started(record)`.
`CreateCallbackOp` is processed in `apply_mutation`'s async
pre-commit phase; `CloseCallbackOp` is processed in commit. The
session-close sweep flips `_closed` on each open callback;
service_refcounts drain handles the rest. No `CallbackReadyEvent`,
no `CallbackErrorEvent`. Bridge failures during pre-commit reject
the mutation as `callback_setup_failed`.

### 5c — `["callback", slot]` codec support (LANDED, reshaped in extra1)

`unescape` accepts an optional `lookup_callback: Callable[[int],
ServiceRef[Any]] | None`. `apply_mutation`'s phase 3 supplies a
session-bound lookup (consults `pending_creates` ∪
`record.open_callbacks`), walks invoke args once, and stores
fully-decoded `resolved_args` for the commit phase. No invoke
worker pre-phase, no `CallbackRecord` placeholder in args.

### 5d — `notify` 3-phase delivery integration

Broken into three reviewable substages. The factored helper has
**raise-on-error semantics**: it always raises if phase-2 bridge
setup or phase-3 encode fails (after decref'ing any in-flight
contributions it made), and the *caller* decides how to surface
the error. For invoke that means catching and emitting an
:class:`InvokeErrorEvent`; for notify it means letting the
exception propagate to the RPC peer.

#### 5d.1 — Add ``CallbackEvent`` + factor the shared helper

- Add :class:`CallbackEvent` to :data:`SessionEvent` (tag
  ``"callback"``, fields ``seq`` / ``slot`` / ``payload``).
- Extract today's `deliver_invoke_result_to_slot` body into a
  shared `_deliver_result_to_slot(state, session_id, slot,
  result, source_scope, *, build_result_event)` helper that:
  - returns ``True`` on commit / ``False`` on aborted
    (session_gone before phase 1 or before phase 3 commit);
  - raises on phase-2 bridge failure or phase-3 encode failure,
    after decref'ing any in-flight contributions it made.
- `deliver_invoke_result_to_slot` becomes a thin wrapper:
  ``try: helper(...) except Exception: emit InvokeErrorEvent``.
  Discards the bool. Existing callers unchanged.
- Drop the now-dead `_phase3_commit_invoke_result` and
  `_phase_rollback_invoke` (their roles split into the helper +
  the wrapper).
- No behavior change: existing invoke-result tests must keep
  passing unchanged.

#### 5d.2 — Wire ``WorkstationCallbackImpl.notify``

- Inside the dispatched method, capture
  ``source_scope = get_active_scope()`` once, then call the
  helper with a `CallbackEvent`-building lambda. Return the
  helper's bool — ``True`` on commit, ``False`` on session-gone,
  exception propagates to the RPC caller.
- Drop the ``NotImplementedError`` branch.
- Add a basic round-trip test: session creates a callback, RPC
  peer receives the ref via an invoke arg, calls ``notify`` with
  a primitive payload, session sees the resulting
  :class:`CallbackEvent`. Verifies ``listening()`` returns
  ``True`` while open.

#### 5d.3 — Tests + docs

- Notify with refs in payload (FxtrID + ServiceRef): phase-1
  attaches; refcounts bump; event payload escapes correctly;
  retention-prune on the result event drops contributions past
  the horizon.
- Cross-session passing: session A creates callback, A invokes a
  service tracked in B with ``["callback", slot]`` arg, B's
  invoke result encodes the ref as ``["service", short_id]``,
  B's invoked code calls ``notify``, A sees a
  :class:`CallbackEvent`.
- Close-from-session: session emits ``close_callback`` op,
  subsequent ``notify`` from peer returns ``False``,
  ``listening()`` returns ``False``, underlying service_scope
  drains.
- Drained-but-alive: session closes the callback while peer
  still holds a bridge; service_scope stays alive; peer's
  ``notify`` / ``listening`` return ``False``; eventually peer
  drops the bridge and the scope fully drains.
- Notify error bubble: force a phase-3 encode error (e.g.
  unencodable value); ``notify`` raises; no event in session
  log; no refcount changes survive.
- Within-mutation create+invoke+close: a single mutation
  ``[CreateCallbackOp(N), InvokeOp(uses ["callback", N]),
  CloseCallbackOp(N)]`` succeeds end-to-end.
- New test fixture: a small ``EventListenerProto`` consumer
  registered as an attached service (RPC peer) that captures a
  callback ref from an invoke arg and exposes a ``notify_now``
  method the test process drives directly.
- Update CODE_OVERVIEW.md.

### 5e — Debug UI + open-slot snapshot fields

Broken into a backend substage and a UI substage. The backend
extension piggy-backs on the existing
:class:`SessionDebugSnapshotEvent` machinery — the snapshot is
already a chatty, sequence-less, full-snapshot push driven by
refcount mutations / `slot_counter` bumps / close-time events, so
we just add three more dict fields and hook emit sites at insert /
remove / clear.

#### 5e.1 — Backend: open-slot snapshot fields

- Add three new fields to :class:`SessionDebugSnapshotEvent`:
  - `open_callbacks: dict[int, str]` — slot → cb send-side
    `service_short_id` (the originally-planned 5e bit).
  - `open_invokes: dict[int, OpenInvokeInfo]` — slot →
    `{service_short_id, method_name}`. New small
    `msgspec.Struct` for the value shape.
  - `open_loads: dict[int, str]` — slot → entity `short_id`.
- Add `open_invokes: dict[int, OpenInvokeInfo]` and
  `open_loads: dict[int, str]` to :class:`SessionRecord` (the
  `open_callbacks` field already exists from 5b).
- Insertion site: `apply_mutation`'s commit phase, immediately
  before each worker spawn (`background_tg.start_soon(
  invoke_worker, ...)` / `load_worker`). This sits inside the
  mutation_context, alongside the arg-ref incref that already
  fires the snapshot via `_emit_session_debug_snapshot`.
- Removal site: each worker's `finally` block (alongside
  `_drop_in_flight_arg_refs`). Wrap the pop + emit in the
  same `mutation_context` the arg-decref already opens. This
  catches success / error / cancellation uniformly.
- Session-close drain: `_close_session_drain_trackers` clears
  `open_invokes` / `open_loads` (open_callbacks is already
  cleared in 5b's sweep).
- Update `_build_session_debug_snapshot` to populate the three
  new fields.
- Tests:
  - Pending invoke shows up in `open_invokes` between commit
    and result-event delivery; clears once the result lands.
  - Pending load shows up in `open_loads` similarly.
  - `open_callbacks` shape after create / close / session-close.
  - Errored invoke / failed load both clear their entry.

#### 5e.2 — Debug UI

- Render `CallbackEvent` rows in the session event log
  (collapsible JSON; payload may carry refs).
- Add an "Open slots" sub-panel to each session debug view —
  three small lists driven by the fields above. Each row shows
  the slot number plus the small description (cb short_id /
  method name / entity short_id).
- Update CODE_OVERVIEW.md.

## Implementation steps

- [x] **5a.** `EventListenerProto` + `WorkstationCallbackImpl` skeleton.
- [x] Update CODE_OVERVIEW.md (after 5a).
- [x] **5b.** `CreateCallbackOp` / `CloseCallbackOp` + lifecycle.
- [x] Update CODE_OVERVIEW.md (after 5b).
- [x] **5c.** `["callback", slot]` codec support.
- [x] Update CODE_OVERVIEW.md (after 5c).
- [x] **(extra1) async-mutation pipeline** — see
  `stage5_extra1_async_mutation.md`. Reshaped 5a-5c.
- [x] **5d.1.** Add `CallbackEvent` + factor the shared helper.
- [x] Update CODE_OVERVIEW.md (after 5d.1).
- [x] **5d.2.** Wire `WorkstationCallbackImpl.notify`.
- [x] Update CODE_OVERVIEW.md (after 5d.2).
- [x] **5d.3.** Tests for refs / close / session-close drain / slot monotonicity / within-mutation create+invoke+close / cross-session passing / drained-but-alive. (Notify-encode-error bubble deferred — hard to engineer naturally.)
- [x] Update CODE_OVERVIEW.md (after 5d.3).
- [x] **5e.1.** Backend: open-slot snapshot fields.
- [x] Update CODE_OVERVIEW.md (after 5e.1).
- [x] **5e.2.** Debug UI (CallbackEvent rendering + open-slots panel).
- [x] Update CODE_OVERVIEW.md (final).

## Open questions / things to revisit

- Memory pressure under a callback that fires very frequently
  with no mutator advancing `ack_seq` — same retention concern
  as load/invoke result events, deferred to phase 0+.
- Whether to factor out `_deliver_result_to_slot` as a shared
  helper between invoke result delivery and notify delivery.
  **Resolved in 5d.1**: factored as
  `_deliver_result_as_event(state, session_id, result,
  source_scope, *, build_result_event)` with raise-on-error
  semantics. Invoke wraps and emits `InvokeErrorEvent` on
  exception; notify lets the exception propagate to the RPC
  caller. (Renamed away from `_to_slot` once `slot` was no
  longer a parameter — only the lambda needs it.)
- Whether the `service_record_worker` and
  `callback_record_worker` should converge into one helper
  parameterized by source-scope ownership ("internal" vs.
  "external"). Open since 5b; defer until we have a third use
  case to drive the abstraction.
- **Within-mutation create+invoke+close behavior is "invoke
  succeeds but notify returns False".** Because
  `close_callback` flips `_closed = True` synchronously in
  commit (step e in the four-phase pipeline), and the invoke
  worker spawns afterward (step f), the worker's eventual
  `notify` call sees the closed flag and short-circuits. The
  in-flight ref keeps the underlying *service record* alive so
  the bridge dispatch doesn't crash, but no `CallbackEvent` is
  committed. This is what the 5d.3 test
  `test_within_mutation_create_invoke_close_succeeds_with_closed_notify`
  pins down. If we ever want "notify-then-close" semantics
  within a single mutation we'd need to defer `_closed`
  flipping until the in-flight contributions drop — not worth
  the complexity unless we hit a real use case.
- **Cross-session passing landed.** ``return_ref(cb)`` on
  ``CallbackInvokerImpl`` round-trips a callback ref so its
  send-side short_id can be read out of the invoke result event.
  The test exercises A creating a cb, surfacing its short_id via
  ``return_ref``, B pinning it via ``track_service``, and B
  firing it — confirming that ``notify`` routes to A's event
  log (carried via ``impl.session_id``), not the invoking
  session's.
- **Drained-but-alive landed.** A peer pulls the cb into its
  own scope via ``WorkstationServiceProto.get_service(cb_short_id)``,
  the session closes the cb and itself, and the peer's
  resolved proxy keeps working — ``listening()`` and
  ``notify("after")`` both return ``False`` (impl was flipped
  ``_closed=True``). After session close,
  ``state.service_records`` no longer contains the cb's
  ``ServiceKey`` and ``lookup_active_service`` returns
  ``None``; the underlying ``service_scope`` lingers until
  the peer drops its ref. (Confirms cb send-sides ride the
  standard ``TrackedServiceRecord`` lifetime story.)
- **Notify encode-error bubble (deferred).** Hard to engineer
  naturally because every value typed as ``FxtrServiceData``
  is a valid input to the escape codec. Would require either
  monkey-patching ``escape`` to raise, or constructing a
  pathological Python object that bypasses the dialect-
  validation path. Skipped for now; the helper's raise-on-
  error contract is exercised indirectly via invoke's
  catch-and-emit-InvokeErrorEvent wrapper path.
