"""Wire protocol for the workstation's ``/client`` endpoint.

A small request/response/event protocol over a single WebSocket. Each
text frame is one JSON object. Stage 2 was read-only over three
resource lists; stage 3a layers in session lifecycle ops (create,
subscribe, unsubscribe, list, subscribe-list); stage 3b.1 adds the
mutation pipeline; stage 3b.2 lands ``close`` as a mutation op
(retiring the stand-in top-level ``close_session``).

The on-wire shape is flat: the discriminator (``op`` for client
requests, ``type`` for server frames) lives at the top level alongside
the message-specific fields.

ID conventions:
- Every client request carries a ``request_id``. The server's reply
  (ack / error / typed response) echoes it as ``request_id`` for
  correlation.
- Every subscription has a ``subscription_id``. Server pushes for a
  subscription (snapshots / events / closed) carry that
  ``subscription_id``.
- For "Subscribe*" requests, the convention is that the request's
  ``request_id`` is *also* used as the new subscription's
  ``subscription_id``. The two values are equal by construction.
- Unsubscribe / mutate requests carry the explicit ``subscription_id``
  of the target subscription, separate from their own ``request_id``.

Examples::

    # client → server
    {"request_id": 1, "op": "subscribe", "resource": "services"}
    {"request_id": 2, "op": "unsubscribe", "subscription_id": 1}
    {"request_id": 3, "op": "create_session", "metadata": {"k": "v"}}
    {"request_id": 4, "op": "subscribe_session", "session_id": "..."}

    # server → client
    {"request_id": 2, "type": "ack"}
    {"request_id": 7, "type": "error", "message": "unknown op"}
    {"subscription_id": 1, "type": "resource_snapshot", "data": [...]}
    {"subscription_id": 1, "type": "resource_changes",
     "added": [...], "updated": [...], "removed": [...]}
    {"request_id": 3, "type": "session_created", "session_id": "..."}
    {"subscription_id": 4, "type": "session_snapshot", "session_id": "...",
     "last_state": {"kind": "state_update", "seq": 0, "ack_seq": 0,
                    "state": {}, "closed": false, "slot_counter": 0,
                    "tracked_entities": [], "tracked_services": []},
     "preserved_events": [], "subsequent_events": []}

Validation is done by ``msgspec`` against the tagged-union schema
defined below.

See ``plans/phase0/part1_ws_server/stage2_client_listing.md`` and
``stage3_sessions.md`` for the design rationale.
"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

import msgspec

# --- Resource kinds ---------------------------------------------------

ResourceKind: TypeAlias = Literal["rpc_connections", "services", "entities", "sessions"]

#: All resources a client can subscribe to. Used by tests; the
#: ``ResourceKind`` Literal type is the source of truth on the wire.
RESOURCE_KINDS: frozenset[ResourceKind] = frozenset(
    {"rpc_connections", "services", "entities", "sessions"}
)


# --- Errors -----------------------------------------------------------


class ProtocolError(ValueError):
    """Raised when an incoming message is malformed.

    Caught at the connection's read loop and surfaced to the peer as
    an :class:`ErrorResponse`. The connection itself stays open.
    """


# --- Client → server requests -----------------------------------------
#
# Tagged union over the ``op`` field. Each request struct contributes
# its own typed fields alongside the discriminator.


class SubscribeRequest(msgspec.Struct, tag="subscribe", tag_field="op", frozen=True):
    """Subscribe to a resource. Per the ID convention, ``request_id``
    doubles as the new subscription's ``subscription_id``.

    ``debug=True`` opts the subscription into debug-only fields on
    each record (e.g. ``pinned_by_sessions``,
    ``registering_connection_ids``) and into change frames triggered
    purely by debug-only field updates. The default keeps production
    consumers off the noisy mutation path. Stage 4a accepts the flag
    and routes it through :class:`ResourceSubscription`; per-record
    projection / change-frame filtering land alongside the
    callsite migration.
    """

    request_id: int
    resource: ResourceKind
    debug: bool = False


class UnsubscribeRequest(msgspec.Struct, tag="unsubscribe", tag_field="op", frozen=True):
    """Cancel a subscription. ``request_id`` is for ack correlation;
    ``subscription_id`` references the subscription being torn down.
    Works uniformly across resource, session, and session-list
    subscriptions."""

    request_id: int
    subscription_id: int


# --- Session lifecycle (stage 3a) -------------------------------------


class CreateSessionRequest(msgspec.Struct, tag="create_session", tag_field="op", frozen=True):
    """Create a new session with the given metadata.

    Replied to with a :class:`SessionCreatedResponse` carrying the
    new session's UUID. Metadata is frozen at creation time —
    anything mutable belongs in session ``state``.
    """

    request_id: int
    metadata: dict[str, Any]


class SubscribeSessionRequest(msgspec.Struct, tag="subscribe_session", tag_field="op", frozen=True):
    """Subscribe to a session by UUID. Per the ID convention,
    ``request_id`` doubles as the new subscription's
    ``subscription_id``."""

    request_id: int
    session_id: str


class SubscribeSessionDebugRequest(
    msgspec.Struct,
    tag="subscribe_session_debug",
    tag_field="op",
    frozen=True,
):
    """Subscribe to a session's debug stream by UUID (stage 4b).

    Mirrors :class:`SubscribeSessionRequest` shape — ``request_id``
    doubles as the ``subscription_id``. The reply is an
    :class:`AckResponse` followed immediately by a
    :class:`SessionDebugSnapshotEvent` with the current refcount
    and slot-counter state. After that, a fresh
    :class:`SessionDebugSnapshotEvent` is pushed on every refcount
    change (explicit pin / unpin, retained-event ref drop,
    in-flight delivery promote/rollback) and on every slot_counter
    bump.

    The debug stream is *separate* from the main session stream
    (``subscribe_session``) — different sequence-numbering rules,
    different update cadence, no event-log replay. Closing the
    session tears down both subscription kinds.
    """

    request_id: int
    session_id: str


# --- Out-of-band entity load (stage 8b) -------------------------------


class LoadEntityRequest(msgspec.Struct, tag="load_entity", tag_field="op", frozen=True):
    """Out-of-band, per-connection direct entity load (stage 8b).

    Not tied to any session — the workstation looks up ``short_id`` in
    its global entity registry, loads the CBOR via a transient scope,
    walks it through the list-based escape codec, and replies with a
    :class:`LoadEntityResponse` (or :class:`LoadEntityErrorResponse`
    on any failure). No session refcounts are touched and no result
    event lands in any session's event log.

    Children FxtrIDs in the payload have a short_id allocated via
    :meth:`fxtr_workstation.short_ids.ShortIDAllocator.get_or_assign_entity_inactive`
    so the descriptors are stable for display, but the children are
    *not* minted as :class:`TrackedEntityRecord`s — they aren't
    lookupable for further direct loads unless something else makes
    them active (e.g. a :class:`TrackEntityOp` against an already-
    tracked parent that exposes them, or a session-bound
    :class:`LoadEntityOp` that goes through the active-records path).

    Intended use: a smart client caches the result client-side and
    pairs it with :class:`LoadEntitySilentOp` (stage 8c) to reproduce
    the session-side refcount side effects without retransmitting the
    payload.
    """

    request_id: int
    short_id: str


# --- Per-callback service-id lookup (stage 10) ------------------------


class GetCallbackServiceRequest(
    msgspec.Struct,
    tag="get_callback_service",
    tag_field="op",
    frozen=True,
):
    """Look up the workstation short_id of the send-side
    :class:`fxtr_workstation.protocols.WorkstationSessionEventInbox`
    service for a callback in the session bound to ``subscription_id``
    (stage 10).

    Synchronous on the workstation: the handler resolves
    ``subscription_id`` to a :class:`SessionSubscription` on the calling
    connection, then looks up ``slot`` in the session's
    ``open_callbacks`` table and returns
    :attr:`CallbackRecord.service_short_id` in a
    :class:`GetCallbackServiceResponse`.

    The agent MCP uses this on session subscribe to discover the
    workstation short_id of the reserved ``workstation_inbox``
    callback so it can mount its ``agent_mcp:{session_id}`` service
    impl with a stable inbox handle. Other callers can use it for
    similar "expose this session-owned callback via /rpc" patterns.

    Errors with :class:`ErrorResponse` (raised + caught by the read
    loop) when ``subscription_id`` isn't a session subscription on
    the calling connection, or when ``slot`` isn't currently present
    in ``session.open_callbacks``.
    """

    request_id: int
    subscription_id: int
    slot: int


# --- Mutation ops (stage 3b.2+) --------------------------------------
#
# Tagged union over the ``kind`` field. Carried inside a
# :class:`MutateSessionRequest`'s ``ops`` array. Stage 3b.2 introduces
# only ``close``; stage 3b.3 / 3c grow the union with tracker / load /
# invoke kinds.


class CloseOp(msgspec.Struct, tag="close", tag_field="kind", frozen=True):
    """Terminal mutation op: close the session after this mutation
    commits. Must be the *only* op in the mutation; rejects if mixed
    with any other op (including tracker / load / invoke ops).

    On commit:
    - The terminal event is a :class:`StateUpdateEvent` with
      ``closed=True``; the mutation's ``patch`` is applied first so
      the carried ``state`` is the final post-patch value (a closing
      mutator can write a "closed because …" annotation into it).
    - The mutator receives a :class:`MutationCommittedResponse` with
      ``seq`` pointing at that terminal event. The mutator's own
      session subscription is silently torn down (the
      ``mutation_committed`` *is* the close signal).
    - Foreign subscribers receive the terminal event in a
      :class:`SessionEventFrame`, then their subscription is torn
      down server-side.
    - All registrations sourced by the session are released
      (entity + service trackers).
    - The session is dropped from the registry. Any later
      :class:`SubscribeSessionRequest` for the same UUID errors.
    - Session-list subscribers receive a fresh snapshot.
    """


class TrackEntityOp(
    msgspec.Struct,
    tag="track_entity",
    tag_field="kind",
    frozen=True,
):
    """Add a session-sourced :class:`EntityRegistration` for the
    workstation entity identified by ``short_id``. The entity must
    already be workstation-tracked (i.e. ``short_id`` resolves to an
    active :class:`TrackedEntityRecord`); rejects otherwise. Rejects
    if the session already tracks this entity.

    The session keeps the entity alive in :attr:`entities_scope` for
    as long as it (the session) is alive — even if every other
    tracker (e.g. an RPC connection) releases their registration."""

    short_id: str


class UntrackEntityOp(
    msgspec.Struct,
    tag="untrack_entity",
    tag_field="kind",
    frozen=True,
):
    """Remove a session-sourced :class:`EntityRegistration` previously
    added by :class:`TrackEntityOp`. Rejects if the session does not
    currently track this entity."""

    short_id: str


class TrackServiceOp(
    msgspec.Struct,
    tag="track_service",
    tag_field="kind",
    frozen=True,
):
    """Add a session-sourced :class:`Registration` for the workstation
    service identified by ``short_id``. The service must already be
    workstation-tracked. Rejects if the session already tracks this
    service."""

    short_id: str


class UntrackServiceOp(
    msgspec.Struct,
    tag="untrack_service",
    tag_field="kind",
    frozen=True,
):
    """Remove a session-sourced :class:`Registration` previously
    added by :class:`TrackServiceOp`. Rejects if the session does
    not currently track this service."""

    short_id: str


class LoadEntityOp(
    msgspec.Struct,
    tag="load_entity",
    tag_field="kind",
    frozen=True,
):
    """Schedule an asynchronous load of the entity identified by
    ``short_id`` and deliver the result into ``slot``.

    Op-time validation:
    - ``short_id`` must resolve to a workstation-tracked entity.
    - ``slot`` must be strictly greater than the session's current
      ``slot_counter``, and must not collide with any other
      load/invoke op's slot in the same mutation.

    On commit:
    - The session's ``slot_counter`` is bumped to the highest claimed
      slot in the mutation.
    - A background ``load_worker`` task is spawned per op. The result
      lands later as a :class:`LoadResultEvent` (or
      :class:`LoadErrorEvent`) at the chosen slot.
    """

    slot: int
    short_id: str


class LoadEntitySilentOp(
    msgspec.Struct,
    tag="load_entity_silent",
    tag_field="kind",
    frozen=True,
):
    """Like :class:`LoadEntityOp` but the result event omits the
    payload (stage 8c).

    Op-time validation, slot rules, and on-commit machinery are
    identical to :class:`LoadEntityOp`. A background ``load_worker``
    task is spawned per op; child entities surfaced by
    ``load_cbor`` are workstation-tracked and the calling session
    takes a refcount on each (released when the result event passes
    the retention horizon — same as the non-silent variant). The
    only difference is in phase 3: instead of escaping the CBOR
    into a wire payload and emitting a :class:`LoadResultEvent`,
    the worker emits a :class:`LoadResultSilentEvent` with no
    payload field.

    Intended use: pair with the out-of-band
    :class:`LoadEntityRequest` (stage 8b) for client-side caching.
    The first time, the client uses :class:`LoadEntityRequest` to
    fetch and cache the bytes; on subsequent re-tracks of the same
    entity, it issues :class:`LoadEntitySilentOp` to reproduce the
    refcount side effects without retransmission.

    Failures still deliver :class:`LoadErrorEvent` (not a silent
    variant) — the error message is small enough to always send.
    """

    slot: int
    short_id: str


class InvokeOp(
    msgspec.Struct,
    tag="invoke",
    tag_field="kind",
    frozen=True,
):
    """Schedule an asynchronous method call on a workstation-tracked
    service and deliver the result into ``slot``.

    ``args`` is a list of list-based-escape-encoded JSON values, one
    per positional argument; ``kwargs`` is the analogous keyword-arg
    mapping (string keys, escape-encoded values). Each value (in
    either slot) can be a primitive, a dict, a literal list (``[[…]]``),
    or a tagged ``["entity", short_id]`` / ``["service", short_id]`` /
    ``["bytes", b64]`` / ``["callback", slot:int]`` reference. The
    callback tag resolves against the *mutating* session's
    currently-open callbacks; the invoke worker awaits the corresponding
    ``CallbackRecord``'s ``ready`` event before substituting in a real
    ``ServiceRef`` pointing at the bridged send-side service.

    ``kwargs`` defaults to ``{}`` for backward wire-compat: older
    mutators that don't ship the field decode to an empty dict, and
    the invoke worker calls ``method(*resolved_args, **resolved_kwargs)``
    which is equivalent to the previous purely-positional path.

    Op-time validation:

    - ``service_short_id`` must resolve to a workstation-tracked
      service.
    - Slot rules same as :class:`LoadEntityOp`.
    - Every entity / service short_id inside ``args`` / ``kwargs``
      must resolve in the workstation registries (anywhere — args
      don't have to be pinned by *this* session).

    On commit a background ``invoke_worker`` task is spawned. The
    result lands later as an :class:`InvokeResultEvent` (or
    :class:`InvokeErrorEvent`) at the chosen slot. Any new entity /
    service refs surfaced by the result become workstation-tracked
    at delivery time and the calling session takes a refcount on
    each (released when the result event passes the retention
    horizon).

    To invoke a method on one of the *session's own callbacks* (e.g.
    ``subscribe_to`` on an inbox), use :class:`CallbackInvokeOp` —
    callbacks are addressed by slot, not short_id.
    """

    slot: int
    service_short_id: str
    method_name: str
    args: list[Any]
    kwargs: dict[str, Any] = msgspec.field(default_factory=dict)


class CallbackInvokeOp(
    msgspec.Struct,
    tag="callback_invoke",
    tag_field="kind",
    frozen=True,
):
    """Schedule an asynchronous method call on one of the session's
    own callbacks (addressed by callback slot) and deliver the result
    into ``slot``.

    The :class:`InvokeOp` variant for callback targets: instead of a
    workstation ``service_short_id``, the target is ``callback_slot``
    — resolved against the session's open callbacks *including any
    created earlier in the same mutation's ops list* (the async
    pre-commit phase brings those fully up before the invoke worker
    spawns, exactly like arg-side ``["callback", slot]`` references).
    This is what lets a client create an inbox and ``subscribe_to`` an
    event source through it in a single mutation, with no
    ``GetCallbackServiceRequest`` round trip.

    Everything else matches :class:`InvokeOp`: same ``args`` /
    ``kwargs`` encoding, same slot-monotonicity rule for ``slot``,
    same result delivery (:class:`InvokeResultEvent` /
    :class:`InvokeErrorEvent`), and the target's send-side service is
    pinned in-flight for the dispatch — so a
    ``[callback_invoke(target=N), close_callback(N)]`` mutation
    commits cleanly (the dispatch then observes the closed inbox).

    Op-time validation: ``callback_slot`` must be open at this point
    in the ops walk (an existing ``open_callbacks`` entry, or a
    ``create_callback`` earlier in this mutation, and not closed by an
    earlier ``close_callback``).
    """

    slot: int
    callback_slot: int
    method_name: str
    args: list[Any]
    kwargs: dict[str, Any] = msgspec.field(default_factory=dict)


class GetEntityMetadataOp(
    msgspec.Struct,
    tag="get_entity_metadata",
    tag_field="kind",
    frozen=True,
):
    """Request synchronous metadata about the entity identified by
    ``short_id`` and deliver the result into ``slot``.

    Synchronous from the client's perspective: the resolution is
    sync inside :meth:`WorkstationState.apply_mutation`'s commit
    phase, and the resulting :class:`EntityMetadataResultEvent`
    lands on the session's event log in the same broadcast window
    as the mutation's ``state_update``. There is no background
    worker.

    Op-time validation:

    - ``short_id`` must resolve to a workstation-tracked entity.
    - ``slot`` must be strictly greater than the session's current
      ``slot_counter`` and every other slot claimed earlier in this
      mutation (same monotonicity rule as :class:`LoadEntityOp`).

    The metadata payload describes the targeted entity's
    :class:`ProvenancedKey` shape. Entity-ref fields are emitted in
    list-escape-tagged form ``["entity", short_id]`` so the frontend's
    ``unescapeWire`` pipeline produces ``EntityRefStub`` instances
    directly (matches the wire shape of load / invoke / callback
    payloads — refs are uniformly tagged). Plain-string fields
    (``id_string``-typed) are emitted bare. Fields populated when
    applicable:

    - ``id_string`` (plain string) — local-only id-string of the data
      half. The provenance half is already surfaced via
      ``provenance_parent`` / ``provenance_parent_id_string`` (when
      present), so emitting the layered form here would be
      redundant.
    - ``unprovenanced`` (``["entity", short_id]``) — ref of
      ``ProvenancedKey(data, None)``; always emitted (creating that
      record on demand if not already present, since short IDs are
      run-lifetime stable).
    - ``provenance_parent`` (``["entity", short_id]``) — ref of
      ``ProvenancedKey(prov, None)``; emitted when the targeted key
      has a non-None ``prov_parent``.
    - ``provenance_parent_id_string`` (plain string) — full id-string
      of the prov parent; emitted alongside ``provenance_parent``.
    - ``unwraps_to`` (``["entity", short_id]``) — ref of the layered
      ``(unwrapped, data)`` form, where ``data`` is the unwrap
      target. Emitted when the data ID has a provenance-node tag
      (``c``/``n``/``p``/``u``) and the arena's ``unwraps_to``
      chain is known.
    - ``unwraps_to_id_string`` (plain string) — full id-string of
      ``unwraps_to``; emitted alongside.
    - ``context_data`` (``["entity", short_id]``) — ref of
      ``ProvenancedKey(c-data, None)``, emitted when the data ID has
      tag ``c`` (the c-node's context data).
    - ``backstep`` (``["entity", short_id]``) — ref of
      ``ProvenancedKey(p-prefix, None)``, emitted when the data ID
      has tag ``p`` and the path has length ≥ 1 (the p-node with
      the last path component dropped).

    Future fields that need to carry a *literal list* as their value
    must wrap with the standard ``[[...]]`` escape; today's fields
    are all plain strings or tagged refs, so no double-list wrapping
    is needed.

    Each short ref in the payload pins its
    :class:`ProvenancedKey` against the session for the lifetime of
    the result event (refcount + ``event_refs[seq]`` sidecar). When
    the event passes the retention horizon, the contributions
    decrement.
    """

    slot: int
    short_id: str


class GetServiceMetadataOp(
    msgspec.Struct,
    tag="get_service_metadata",
    tag_field="kind",
    frozen=True,
):
    """Request synchronous metadata about the service identified by
    ``short_id``.

    Op-time validation:

    - ``short_id`` must resolve to a workstation-tracked service.
    - ``slot`` follows the same monotonicity rule as
      :class:`LoadEntityOp`.

    The metadata payload currently exposes a single field —
    ``type_id_string`` (the service's ``service_type_id``'s
    id-string). The op shape mirrors :class:`GetEntityMetadataOp`
    so future fields slot in without protocol churn.
    """

    slot: int
    short_id: str


class CreateCallbackOp(
    msgspec.Struct,
    tag="create_callback",
    tag_field="kind",
    frozen=True,
):
    """Open the receive side of a callback at ``slot``.

    Op-time validation:

    - ``slot`` must be strictly greater than the session's current
      ``slot_counter`` and every other slot claimed earlier in this
      mutation (same monotonicity rule as :class:`LoadEntityOp` /
      :class:`InvokeOp`).

    The workstation's async pre-commit phase brings the callback up
    *before* the mutation's :class:`StateUpdateEvent` is broadcast.
    Concretely it spawns a ``callback_record_worker`` for the slot
    via ``task_group.start(...)``, which opens an isolated
    ``impl_scope``, mounts a
    :class:`~fxtr_workstation.callback_impl.WorkstationInboxImpl`,
    brings up a :class:`TrackedServiceRecord`, awaits the bridge,
    and signals readiness back to the caller. By the time the
    mutation commits, ``["callback", slot]`` references resolve to
    a fully-bridged :class:`~fxtr2.data.entity_ref_scope.ServiceRef`.

    Bridge failure during pre-commit rejects the whole mutation as
    :class:`MutationRejectedResponse(reason="callback_setup_failed")`
    — bridge failures on a workstation-internal service indicate a
    bug rather than a user-facing error mode.

    Slot numbers are consumed for the rest of the session even if
    the callback is later closed.
    """

    slot: int


class PreserveEventsOp(
    msgspec.Struct,
    tag="preserve_events",
    tag_field="kind",
    frozen=True,
):
    """Preserve a batch of session events past the ack horizon (stage 8a).

    Each ``seq`` in ``seqs`` must be a result/notification event currently in
    the session's ``event_log`` (i.e. ``seq > old last_state_ack_seq``) that is
    being acknowledged by *this* mutation (``seq <= new ack_seq`` — would
    otherwise drop in retention). State updates are not preservable; rejects
    if any seq targets a :class:`StateUpdateEvent`. Rejects if any seq is
    already preserved or appears more than once in ``seqs``.

    On commit, the preserved events are moved out of ``event_log`` into the
    session's ``preserved_events`` sidecar; their ``event_refs[seq]`` entries
    stay intact (so child entity / service refcounts remain held). Preserved
    events are re-delivered alongside the unacked suffix on resubscribe; the
    receiver distinguishes them by comparing ``seq`` to ``last_state.ack_seq``.

    Mixing :class:`PreserveEventsOp` with :class:`UnpreserveEventsOp` in the
    same mutation is allowed; ``[Preserve(seq), Unpreserve(seq)]`` nets to
    no-op (the event remains in ``event_log`` and is dropped by the same
    mutation's retention prune).
    """

    seqs: list[int]


class UnpreserveEventsOp(
    msgspec.Struct,
    tag="unpreserve_events",
    tag_field="kind",
    frozen=True,
):
    """Drop a batch of previously-preserved session events (stage 8a).

    Each ``seq`` in ``seqs`` must currently be in the session's
    ``preserved_events`` sidecar (taking into account in-mutation working
    state from earlier ops in the same ``ops`` list). Rejects if any seq is
    not currently preserved or appears more than once in ``seqs``.

    On commit, each seq is popped from ``preserved_events`` and its
    ``event_refs[seq]`` contributions are decref'd against the session
    (releasing entity / service records that no other holders pin).
    """

    seqs: list[int]


class CloseCallbackOp(
    msgspec.Struct,
    tag="close_callback",
    tag_field="kind",
    frozen=True,
):
    """Close the receive side of the callback at ``slot``.

    Closing the receive side is independent of the send side: any
    third-party send-side track on the underlying service short_id
    (via :class:`TrackServiceOp`) survives this op. Once closed, the
    impl's ``listening()`` returns ``False`` and ``notify`` is a
    silent no-op even if external bridges keep the service alive.

    Op-time validation: ``slot`` must currently be present in the
    session's ``open_callbacks``. Mixing ``close_callback`` with other
    ops in the same mutation is allowed (unlike the terminal
    :class:`CloseOp`).

    On commit, the workstation pops the slot from the session's
    ``open_callbacks``, calls ``impl.mark_closed()`` (flips
    ``_closed`` — so external bridges' next ``notify`` silently drops
    and ``listening`` returns ``False`` — and wakes the inbox's
    subscription parker tasks, which unsubscribe from their
    ``subscribe_to`` sources and tear the subscriptions down), and
    drops the session's open-callback refcount contribution. If no
    other holders remain, the underlying
    :class:`TrackedServiceRecord` is released and the worker drains.
    """

    slot: int


# Tagged union of mutation ops.
MutationOp: TypeAlias = (
    CloseOp
    | TrackEntityOp
    | UntrackEntityOp
    | TrackServiceOp
    | UntrackServiceOp
    | LoadEntityOp
    | LoadEntitySilentOp
    | InvokeOp
    | CallbackInvokeOp
    | GetEntityMetadataOp
    | GetServiceMetadataOp
    | CreateCallbackOp
    | CloseCallbackOp
    | PreserveEventsOp
    | UnpreserveEventsOp
)


# --- Mutation (stage 3b.1+) -------------------------------------------


class MutateSessionRequest(
    msgspec.Struct,
    tag="mutate_session",
    tag_field="op",
    frozen=True,
):
    """Mutate a session's state via a JSON Patch (RFC 6902) and an
    optional sequence of side-effect ops.

    On success the mutator receives a :class:`MutationCommittedResponse`
    correlated to ``request_id``; **not** a :class:`SessionEventFrame`
    for the resulting ``state_update`` (self-suppression). All other
    subscribers to the session see the ``state_update`` event in
    their stream.

    Stale-ack rule: ``ack_seq`` must be ``>= session.last_state_ack_seq``
    (monotonic non-decreasing). If ``ack_seq < session.last_state_seq``
    and the most recent state-update was made by a *different*
    ``(connection, subscription_id)`` pair, the mutation is rejected.
    Same-mutator skip-ahead is allowed.

    Atomicity: any patch or op failure rejects the whole mutation —
    no events appended, no state mutated, no slots consumed.

    Stage 3b.2 introduces the ``close`` op; stage 3b.3 introduces
    tracker ops; stage 3c introduces ``load_entity`` / ``invoke``.
    """

    request_id: int
    subscription_id: int
    ack_seq: int
    update_id: int
    patch: list[dict[str, Any]]
    ops: list[MutationOp]


ClientRequest: TypeAlias = (
    SubscribeRequest
    | UnsubscribeRequest
    | CreateSessionRequest
    | SubscribeSessionRequest
    | SubscribeSessionDebugRequest
    | MutateSessionRequest
    | LoadEntityRequest
    | GetCallbackServiceRequest
)


# --- Server → client frames -------------------------------------------
#
# Tagged union over the ``type`` field.


class AckResponse(msgspec.Struct, tag="ack", tag_field="type", frozen=True):
    """Successful response to a request. ``request_id`` correlates
    with the originating request."""

    request_id: int


class ErrorResponse(msgspec.Struct, tag="error", tag_field="type", frozen=True):
    """Error response to a request. Connection stays open."""

    request_id: int
    message: str


class ResourceSnapshotEvent(msgspec.Struct, tag="resource_snapshot", tag_field="type", frozen=True):
    """Initial snapshot push for an active resource subscription.

    Sent once after the :class:`AckResponse` for a
    :class:`SubscribeRequest`. ``data`` is the full current list of
    records for the resource, projected against the subscription's
    ``debug`` flag (debug-only fields are stripped for non-debug
    subscribers — projection lands with the callsite migration; for
    now both projections are identical).

    After the initial snapshot, the server sends incremental
    :class:`ResourceChangesEvent` frames as the resource list mutates.
    """

    subscription_id: int
    data: list[dict[str, Any]]


class ResourceChangesEvent(msgspec.Struct, tag="resource_changes", tag_field="type", frozen=True):
    """Incremental delta push for an active resource subscription.

    Emitted at the boundary of each top-level state mutation (one
    frame per affected resource per subscriber). ``added`` and
    ``updated`` carry the full record dicts (projected against the
    subscription's ``debug`` flag); ``removed`` carries just the
    record key (``connection_id`` / ``short_id`` / ``session_id``
    depending on the resource).

    A frame with all three lists empty is suppressed; non-debug
    subscribers also suppress frames whose only changes are to
    debug-only fields. WebSocket guarantees per-connection frame
    ordering, so the delta stream is interpreted strictly relative
    to the preceding snapshot + deltas on the same subscription —
    no sequence numbers.
    """

    subscription_id: int
    added: list[dict[str, Any]]
    updated: list[dict[str, Any]]
    removed: list[str]


# --- Session events (stage 3b.1+) -------------------------------------
#
# Tagged union over ``kind``. Carried inside a :class:`SessionEventFrame`
# as a server push, and inside :class:`SessionSnapshotEvent.events` as
# the unacked-suffix replay.


class StateUpdateEvent(msgspec.Struct, tag="state_update", tag_field="kind", frozen=True):
    """A successful mutation's state-update event. ``ack_seq`` is the
    ack value the mutator carried (i.e. the seq up to which the
    mutator had processed the log when they sent the mutation).

    ``closed`` is set when the originating mutation included a
    :class:`CloseOp`. In that case, this is the *terminal* event of
    the session: foreign subscribers should treat their subscription
    as torn down after this frame, and the session is gone from the
    registry. The carried ``state`` is still meaningful — a closing
    mutator can e.g. write a "closed because …" annotation into
    state for archival readers, even though no one will subscribe
    again.

    ``tracked_entities`` / ``tracked_services`` (stage 4b) are the
    short_ids of the session's explicit pins *after* the mutation
    commits — i.e. ``session.tracked_entities`` and
    ``session.tracked_services`` projected through the workstation's
    short-id allocator. Sorted for deterministic wire shape. For a
    terminal close event, both are empty (session-close drain runs
    before event construction). These fields are set on every
    ``state_update`` event including the synthetic seq=0 stand-in
    used by :class:`SessionSnapshotEvent.last_state` for sessions
    with no committed mutations (where they're empty by definition).

    ``slot_counter`` (stage 3-post-2) is the session's slot-counter
    value *after* the mutation commits. Lets clients track the
    workstation's slot counter without re-fetching a snapshot: every
    state_update both at commit time and as the snapshot's
    ``last_state`` carries the up-to-date value. ``0`` for the
    synthetic ``seq=0`` stand-in (a fresh session has consumed no
    slots).

    State_updates are not persisted in :attr:`SessionRecord.event_log`
    — they're authoritative through :attr:`SessionRecord.last_state_seq`
    / :attr:`SessionRecord.last_state_ack_seq` /
    :attr:`SessionRecord.state` / :attr:`SessionRecord.slot_counter`
    and broadcast in-the-moment via :class:`SessionEventFrame`. They
    appear on the wire only as :attr:`SessionEventFrame.event` for
    live broadcasts and as :attr:`SessionSnapshotEvent.last_state`
    for snapshots — never inside a snapshot's
    :attr:`SessionSnapshotEvent.preserved_events` or
    :attr:`SessionSnapshotEvent.subsequent_events` buckets.
    """

    seq: int
    ack_seq: int
    state: dict[str, Any]
    closed: bool = False
    tracked_entities: list[str] = msgspec.field(default_factory=list)
    tracked_services: list[str] = msgspec.field(default_factory=list)
    slot_counter: int = 0


class LoadResultEvent(msgspec.Struct, tag="load_result", tag_field="kind", frozen=True):
    """Result event for a successful :class:`LoadEntityOp`.

    ``slot`` matches the ``slot`` requested by the originating
    mutation. ``payload`` is the loaded entity's CBOR walked through
    the list-based escape codec — entity references inside the CBOR
    appear as ``["entity", "<short-id>"]`` tags, ``bytes`` fields as
    ``["bytes", "<base64>"]``, etc. (See
    :mod:`fxtr_workstation.wire_escape`.)

    The referenced entities are workstation-tracked at delivery time
    and the session takes a refcount on each. Those refcounts drop
    when this event passes the retention horizon.
    """

    seq: int
    slot: int
    payload: Any


class LoadResultSilentEvent(
    msgspec.Struct, tag="load_result_silent", tag_field="kind", frozen=True
):
    """Result event for a successful :class:`LoadEntitySilentOp` (8c).

    Carries no payload. Refcount side effects are identical to
    :class:`LoadResultEvent` — child entities surfaced by
    ``load_cbor`` are workstation-tracked and refcounted against
    the calling session via the same ``event_refs[seq]`` machinery,
    so the event participates in retention / preservation just like
    :class:`LoadResultEvent`. ``slot`` and ``seq`` follow the
    standard slot/result-event semantics.
    """

    seq: int
    slot: int


class LoadErrorEvent(msgspec.Struct, tag="load_error", tag_field="kind", frozen=True):
    """Failure event for a :class:`LoadEntityOp`. Delivered when the
    workstation could not load the entity (e.g. ``load_cbor`` raised)
    or the result could not be encoded for the wire."""

    seq: int
    slot: int
    message: str


class InvokeResultEvent(msgspec.Struct, tag="invoke_result", tag_field="kind", frozen=True):
    """Result event for a successful :class:`InvokeOp` /
    :class:`CallbackInvokeOp`.

    ``slot`` matches the ``slot`` requested by the originating
    mutation. ``payload`` is the dispatched method's return value
    walked through the list-based escape codec — entity / service
    refs surfaced by the result appear as ``["entity", short_id]`` /
    ``["service", short_id]`` tags, ``bytes`` fields as
    ``["bytes", "<base64>"]``, etc.

    Any new entity / service refs in the payload become workstation-
    tracked at delivery time and the session takes a refcount on
    each. Those refcounts drop when this event passes the retention
    horizon.
    """

    seq: int
    slot: int
    payload: Any


class InvokeErrorEvent(msgspec.Struct, tag="invoke_error", tag_field="kind", frozen=True):
    """Failure event for an :class:`InvokeOp` /
    :class:`CallbackInvokeOp`. Delivered when the
    dispatched method raised, when a service-bridge for a result-
    side ``ServiceRef`` could not complete, or when the result
    could not be encoded for the wire."""

    seq: int
    slot: int
    message: str


class EntityMetadataResultEvent(
    msgspec.Struct, tag="entity_metadata_result", tag_field="kind", frozen=True
):
    """Result event for a successful :class:`GetEntityMetadataOp`.

    ``slot`` matches the originating mutation. ``metadata`` is a
    dict with the fields described on :class:`GetEntityMetadataOp`;
    entity refs appear in list-escape-tagged form
    ``["entity", short_id]`` (matching load / invoke / callback
    payload conventions), while plain-string fields (``id_string``,
    ``provenance_parent_id_string``, ``unwraps_to_id_string``) are
    emitted bare. Any ref surfaced as a value pins its
    :class:`ProvenancedKey` against the session for the lifetime of
    this event (refcount + ``event_refs[seq]`` sidecar).
    """

    seq: int
    slot: int
    metadata: dict[str, Any]


class EntityMetadataErrorEvent(
    msgspec.Struct, tag="entity_metadata_error", tag_field="kind", frozen=True
):
    """Failure event for a :class:`GetEntityMetadataOp`. Delivered
    when the targeted ``short_id`` is unresolvable at commit time
    (e.g. the record was released between op-validation and the
    commit phase across a callback-creation await)."""

    seq: int
    slot: int
    message: str


class ServiceMetadataResultEvent(
    msgspec.Struct, tag="service_metadata_result", tag_field="kind", frozen=True
):
    """Result event for a successful :class:`GetServiceMetadataOp`.
    ``metadata`` carries a single plain-string ``type_id_string`` field
    today; additional fields can be added without protocol churn. Any
    future ref-valued fields would be emitted in list-escape-tagged
    form (``["entity", short_id]`` / ``["service", short_id]``) to
    match the conventions on :class:`EntityMetadataResultEvent` and
    other workstation result events."""

    seq: int
    slot: int
    metadata: dict[str, Any]


class ServiceMetadataErrorEvent(
    msgspec.Struct, tag="service_metadata_error", tag_field="kind", frozen=True
):
    """Failure event for a :class:`GetServiceMetadataOp`."""

    seq: int
    slot: int
    message: str


class CallbackEvent(msgspec.Struct, tag="callback", tag_field="kind", frozen=True):
    """Result event for a successful event delivery into a
    workstation-minted callback inbox (the receive side of
    :class:`~fxtr_workstation.protocols.WorkstationSessionEventInbox`)
    — either a direct-push ``notify`` or a subscription delivery
    (``subscribe_to`` → the source's ``on_event`` dispatch).

    ``slot`` is the receive-side slot bound by the originating
    :class:`CreateCallbackOp`. ``payload`` is the delivered value
    walked through the list-based escape codec — entity / service refs
    in the payload appear as ``["entity", short_id]`` /
    ``["service", short_id]`` tags, ``bytes`` fields as
    ``["bytes", "<base64>"]``, etc.

    Any new entity / service refs in the payload become workstation-
    tracked at delivery time and the session takes a refcount on
    each. Those refcounts drop when this event passes the retention
    horizon. There is no companion ``CallbackErrorEvent``: phase-2 /
    phase-3 failures inside the delivery pipeline raise out of
    ``notify`` to the RPC peer rather than landing on the session's
    event log.
    """

    seq: int
    slot: int
    payload: Any


class CallbackSubscriptionEvent(
    msgspec.Struct, tag="callback_subscription", tag_field="kind", frozen=True
):
    """Connection-state transition for a workstation-minted callback
    inbox's ``subscribe_to`` subscription.

    A ``subscribe_to`` subscription is owned end-to-end by the
    workstation (see
    :mod:`fxtr_workstation.callback_impl`). This event reports a
    transition in that subscription's connection state to the inbox's
    owning session:

    - ``connected=True`` — a subscription was successfully established
      (``subscribe_to`` brought it fully up). ``reason`` is ``None``.
    - ``connected=False`` — the live subscription **broke** ungracefully
      (the source's grant was revoked / its RPC connection dropped, so
      the inbox has stopped receiving events). ``reason`` carries the
      keep-alive lease's break-reason string for display.

    Unlike :class:`ServiceBrokenNotification` (a sequence-less side
    frame for *terminal* service brokenness), this is a normal seq'd
    :class:`NonStateSessionEvent`: a subscription break is **recoverable**
    (the client can ``subscribe_to`` a fresh source on the same slot),
    so it is modeled as a state *transition* the client reduces into a
    per-inbox connection state — the same way it processes
    :class:`CallbackEvent`\\ s. The latest event for a ``slot`` wins;
    the absence of any such event means "idle" (never subscribed). An
    **orderly** inbox close does *not* emit ``connected=False`` — that
    is covered by the close path itself.

    ``slot`` is the receive-side slot bound by the originating
    :class:`CreateCallbackOp`. Because an inbox permits at most one live
    subscription at a time, the event needs no per-subscription
    identifier.
    """

    seq: int
    slot: int
    connected: bool
    reason: str | None = None


NonStateSessionEvent: TypeAlias = (
    LoadResultEvent
    | LoadResultSilentEvent
    | LoadErrorEvent
    | InvokeResultEvent
    | InvokeErrorEvent
    | EntityMetadataResultEvent
    | EntityMetadataErrorEvent
    | ServiceMetadataResultEvent
    | ServiceMetadataErrorEvent
    | CallbackEvent
    | CallbackSubscriptionEvent
)
"""Tagged union of session events *other than* :class:`StateUpdateEvent`.

These are the events that live in :attr:`SessionRecord.event_log`
and :attr:`SessionRecord.preserved_events`. They are also the only
events that appear inside a snapshot's
:attr:`SessionSnapshotEvent.preserved_events` /
:attr:`SessionSnapshotEvent.subsequent_events` buckets.
"""


SessionEvent: TypeAlias = StateUpdateEvent | NonStateSessionEvent
"""Tagged union of all session events.

Callback creation has no dedicated session event: the workstation's
async pre-commit phase brings the callback up *before* the
mutation's :class:`StateUpdateEvent` commits, so the callback is
fully usable by the time subscribers see the state-update. A bridge
failure during pre-commit rejects the whole mutation as
:class:`MutationRejectedResponse(reason="callback_setup_failed")`
rather than emitting a per-event error.

Successful ``notify`` invocations against a workstation-minted
callback land as :class:`CallbackEvent` in the originating session's
event log. Failures inside the ``notify`` delivery pipeline (phase-2
bridge errors / phase-3 encode errors) raise out to the RPC peer
rather than producing a session-side error event — the workstation's
own bridge is internal infrastructure, and the caller (the peer
that holds the send-side ref) is the responsible party.
"""


# --- Session frames (stage 3a / 3b.1) ---------------------------------


class SessionCreatedResponse(
    msgspec.Struct,
    tag="session_created",
    tag_field="type",
    frozen=True,
):
    """Response to a successful :class:`CreateSessionRequest`.
    ``session_id`` is the new session's UUID (string-encoded)."""

    request_id: int
    session_id: str


class MutationCommittedResponse(
    msgspec.Struct,
    tag="mutation_committed",
    tag_field="type",
    frozen=True,
):
    """Mutator-only response to a successful :class:`MutateSessionRequest`.

    ``request_id`` correlates with the mutate request. ``subscription_id``
    and ``update_id`` are echoed back for client-side bookkeeping.
    ``seq`` is the seq of the resulting ``state_update`` event so the
    mutator knows their commit's position in the log.

    The mutator does **not** also receive a :class:`SessionEventFrame`
    for this commit; foreign subscribers do (self-suppression).
    """

    request_id: int
    subscription_id: int
    seq: int
    update_id: int


#: Reason codes for :class:`MutationRejectedResponse`. Each code
#: identifies a distinct mutation-pipeline failure mode the client
#: may want to handle differently:
#:
#: - ``stale_ack``: ``ack_seq`` is below the session's retention
#:   horizon (``ack_seq < session.last_state_ack_seq``). Re-fetch
#:   the latest state and retry.
#: - ``conflict``: ``ack_seq < session.last_state_seq`` and the last
#:   accepted mutation came from a different
#:   ``(connection_id, subscription_id)``. Re-fetch and retry.
#: - ``validation_failed``: op validation rejected the mutation
#:   (unknown short_id, non-monotonic slot, malformed args, JSON
#:   patch failure, etc.). The ``message`` carries the specific
#:   failure; the mutation should not be retried verbatim.
#: - ``callback_setup_failed``: a :class:`CreateCallbackOp` couldn't
#:   bring its bridge up. Should be very rare — workstation-internal
#:   bridge.
#: - ``session_busy``: the session's mutation queue was full. Retry
#:   later.
#: - ``session_gone``: the session was closed before this mutation
#:   could be applied.
MutationRejectReason: TypeAlias = Literal[
    "stale_ack",
    "conflict",
    "validation_failed",
    "callback_setup_failed",
    "session_busy",
    "session_gone",
]


class MutationRejectedResponse(
    msgspec.Struct,
    tag="mutation_rejected",
    tag_field="type",
    frozen=True,
):
    """Response to a :class:`MutateSessionRequest` that was rejected
    before commit.

    Distinct from :class:`ErrorResponse`, which still handles
    transport-level failures (malformed JSON, unknown ops, missing
    required fields). All mutation-pipeline rejections — stale ack,
    op validation failure, JSON-patch failure, callback bridge
    failure, queue backpressure, session-already-closed — surface
    here with a typed :data:`MutationRejectReason` so clients can
    dispatch on the failure kind without parsing free-form messages.

    ``request_id`` correlates with the mutate request. ``message``
    carries human-readable detail (always populated for
    ``validation_failed`` / ``callback_setup_failed``; usually
    populated for ``stale_ack`` / ``conflict``; may be empty for
    ``session_busy`` / ``session_gone``).
    """

    request_id: int
    reason: MutationRejectReason
    message: str = ""


class SessionSnapshotEvent(
    msgspec.Struct,
    tag="session_snapshot",
    tag_field="type",
    frozen=True,
):
    """Initial state push for a session subscription.

    ``last_state`` carries the most recent :class:`StateUpdateEvent`
    (synthetic, ``seq=0 ack_seq=0 state={}`` for a session with no
    committed mutations). State-related fields like ``slot_counter``
    and the explicit-pin sets live on ``last_state`` — there's no
    redundant top-level copy.

    The non-state event log is split into two explicit buckets, both
    sorted by ``seq``:

    - :attr:`preserved_events`: events from the session's
      ``preserved_events`` sidecar (events held past the retention
      horizon by an earlier :class:`PreserveEventsOp`). Each has
      ``seq <= last_state.ack_seq``.
    - :attr:`subsequent_events`: events from the session's
      ``event_log`` (the unacked suffix). Each has
      ``seq > last_state.ack_seq``.

    Neither bucket ever contains a :class:`StateUpdateEvent` — the
    type is :class:`NonStateSessionEvent`.
    """

    subscription_id: int
    session_id: str
    last_state: StateUpdateEvent
    preserved_events: list[NonStateSessionEvent]
    subsequent_events: list[NonStateSessionEvent]


class SessionEventFrame(
    msgspec.Struct,
    tag="session_event",
    tag_field="type",
    frozen=True,
):
    """Server push for a session subscription."""

    subscription_id: int
    event: SessionEvent


class OpenInvokeInfo(msgspec.Struct, frozen=True):
    """Per-slot description of a pending invoke (5e.1).

    Carried inside :attr:`SessionDebugSnapshotEvent.open_invokes` —
    the workstation tracks the dispatched-to service's short_id and
    the method name so the debug UI can show "what call is in flight"
    for each slot, without needing to dig through the in-flight
    refcount dicts to figure it out.
    """

    service_short_id: str
    method_name: str


class SessionDebugSnapshotEvent(
    msgspec.Struct,
    tag="session_debug_snapshot",
    tag_field="type",
    frozen=True,
):
    """Full-snapshot push for a :class:`SubscribeSessionDebugRequest`
    subscription (stage 4b).

    Sent once on subscribe and re-sent on every refcount or
    ``slot_counter`` change, plus on every change to the open-slot
    dicts (5e.1). Full snapshot rather than delta because the data
    is small (per-session refcount + open-slot dicts are typically
    dozens of keys at most) and the simplification is worth more
    than the bandwidth — this is a debug-only stream.

    ``entity_refcounts`` / ``service_refcounts`` map workstation
    short_ids to integer counts; the count is the sum of explicit
    pins + retained event refs + in-flight delivery contributions
    held by this session for that ref. ``slot_counter`` is the
    session's high-water mark for client-allocated slot numbers.

    ``open_callbacks`` / ``open_invokes`` / ``open_loads`` (5e.1)
    each map a slot number to a small descriptor of the
    workstation-side operation that's still in flight for that slot
    — the cb's send-side service short_id, the dispatched-to invoke
    target + method name, or the entity short_id being loaded.
    Entries appear at commit time (open_callbacks at attach;
    open_invokes / open_loads at worker-spawn) and are removed when
    the corresponding slot's terminal event commits (close_callback
    / result-or-error event). Session close clears them.

    ``preserved_events`` (8a) is the sorted list of seqs the session
    is explicitly holding past the retention horizon via
    :class:`PreserveEventsOp`. The corresponding events themselves
    live on the server in ``SessionRecord.preserved_events`` and
    re-appear in ``SessionSnapshotEvent.events`` on resubscribe; the
    debug stream surfaces just the seqs so consumers can render
    "this event is preserved" badges without parsing the full event
    body.
    """

    subscription_id: int
    session_id: str
    entity_refcounts: dict[str, int]
    service_refcounts: dict[str, int]
    slot_counter: int
    open_callbacks: dict[int, str] = msgspec.field(default_factory=dict)
    open_invokes: dict[int, OpenInvokeInfo] = msgspec.field(default_factory=dict)
    open_loads: dict[int, str] = msgspec.field(default_factory=dict)
    preserved_events: list[int] = msgspec.field(default_factory=list)


# --- Out-of-band entity load responses (stage 8b) -------------------


class LoadEntityResponse(msgspec.Struct, tag="load_entity_response", tag_field="type", frozen=True):
    """Successful reply to a :class:`LoadEntityRequest`. ``payload`` is
    the loaded entity's CBOR walked through the list-based escape codec
    — same wire shape as :class:`LoadResultEvent.payload`. ``request_id``
    correlates with the originating request."""

    request_id: int
    payload: Any


class LoadEntityErrorResponse(
    msgspec.Struct,
    tag="load_entity_error",
    tag_field="type",
    frozen=True,
):
    """Failure reply to a :class:`LoadEntityRequest`. Covers any
    failure mode: unknown short_id, ``load_cbor`` raised, or wire
    encoding failed. ``message`` is a free-form string (typically
    ``repr(exc)`` for caught exceptions)."""

    request_id: int
    message: str


# --- Per-callback service-id lookup response (stage 10) ---------------


class GetCallbackServiceResponse(
    msgspec.Struct,
    tag="get_callback_service",
    tag_field="type",
    frozen=True,
):
    """Successful reply to :class:`GetCallbackServiceRequest`.

    ``service_short_id`` is the workstation short_id of the
    callback's send-side service (``s{N}``-style). The caller can
    then resolve it via the existing ``get_service(short_id)``
    surface on :class:`WorkstationServiceProto`.

    Failures use the generic :class:`ErrorResponse`; there's no
    dedicated error-response type for this request kind.
    """

    request_id: int
    subscription_id: int
    slot: int
    service_short_id: str


class ServiceBrokenNotification(
    msgspec.Struct,
    tag="service_broken",
    tag_field="type",
    frozen=True,
):
    """Out-of-band notice that a service a session holds has broken.

    Delivered on a session subscription when, from that subscription's
    perspective, a service the session currently holds
    (``service_refcounts[key] > 0`` — explicit ``TrackServiceOp`` pins
    *and* implicit refs from unacked / preserved event payloads /
    in-flight deliveries) has become broken: the grant backing the
    workstation's bridge to that service was revoked or its RPC
    connection dropped, so every operation on it now fails.

    This is **not** a session event: it carries no seq, is never
    appended to :attr:`SessionRecord.event_log`, never participates in
    the ack / retention machinery, and is never replayed through a
    snapshot. Service brokenness is persistent *state*, not a
    point-in-time event — modeling it as a seq'd event would silently
    lose it once acked or for a subscriber that arrives after the
    break. Instead the workstation re-derives and re-delivers it from
    current state on every (re)subscribe, deduped per subscription, so
    "broke while I was away / before I subscribed" is handled by
    construction.

    Breakage is one-way and terminal: a client that wants a working
    service obtains a fresh ref. The notification does not change the
    service's tracked status or lifecycle; the service stays *known*
    to the workstation (its short_id keeps resolving) but is unusable
    until released.
    """

    subscription_id: int
    service_short_id: str


ServerFrame: TypeAlias = (
    AckResponse
    | ErrorResponse
    | ResourceSnapshotEvent
    | ResourceChangesEvent
    | SessionCreatedResponse
    | SessionSnapshotEvent
    | MutationCommittedResponse
    | MutationRejectedResponse
    | SessionEventFrame
    | SessionDebugSnapshotEvent
    | LoadEntityResponse
    | LoadEntityErrorResponse
    | GetCallbackServiceResponse
    | ServiceBrokenNotification
)


# --- Parse / serialize ------------------------------------------------
#
# msgspec handles structural validation (required fields, types, tag
# discriminator). We translate its exceptions to :class:`ProtocolError`
# so the call site has a single error type to catch.

_REQUEST_DECODER = msgspec.json.Decoder(ClientRequest)
_FRAME_ENCODER = msgspec.json.Encoder()


def parse_text(text: str) -> ClientRequest:
    """Parse a raw WebSocket text frame into a :class:`ClientRequest`.

    Raises :class:`ProtocolError` for any structural issue (malformed
    JSON, unknown op, type mismatch, missing required field, etc.).
    """
    try:
        return _REQUEST_DECODER.decode(text)
    except msgspec.ValidationError as e:
        raise ProtocolError(str(e)) from e
    except msgspec.DecodeError as e:
        raise ProtocolError(f"invalid JSON: {e}") from e


def serialize_to_text(frame: ServerFrame) -> str:
    """Encode a :class:`ServerFrame` as a WebSocket text frame."""
    return _FRAME_ENCODER.encode(frame).decode()
