"""In-memory workstation state.

Stage 1 grows the state with:

- :class:`Registration` — a single binding of a tracked service to an
  external source (currently always an ``RPCConnectionRecord``;
  future stages add workstation-internal sources).
- :class:`TrackedServiceRecord` — one record per :class:`ServiceKey`
  the workstation knows about. Owns a per-record ``service_scope``
  managed by a worker task that opens the scope, bridges the
  registered service into it, and waits on a shutdown event before
  the scope drains. ``get_service`` calls bridge from
  ``service_scope`` into the requester's call scope, so the bridge
  in ``service_scope`` is the workstation's stable identity for the
  service.

**Scope discipline.** All scopes used here are ``isolated=True`` with
explicit ``include=`` parents. ``WorkstationState.root_scope`` is the
workstation's primary scope; per-connection and per-service scopes
both ``include`` it. No reliance on the caller's active scope. See
``v2/notes/scope_discipline.md``.
"""

from __future__ import annotations

import datetime
import logging
import traceback
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeAlias
from uuid import UUID, uuid4

import anyio
import anyio.abc
import anyio.streams.memory
from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, ServiceKey, ServiceRef
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.util.anyio_util import create_task_group

_log = logging.getLogger(__name__)

import jsonpatch

from .callback_impl import WorkstationInboxImpl
from .client_protocol import (
    CallbackInvokeOp,
    CallbackSubscriptionEvent,
    CloseCallbackOp,
    CloseOp,
    CreateCallbackOp,
    EntityMetadataErrorEvent,
    EntityMetadataResultEvent,
    GetEntityMetadataOp,
    GetServiceMetadataOp,
    InvokeErrorEvent,
    InvokeOp,
    InvokeResultEvent,
    LoadEntityErrorResponse,
    LoadEntityOp,
    LoadEntityResponse,
    LoadEntitySilentOp,
    LoadErrorEvent,
    LoadResultEvent,
    LoadResultSilentEvent,
    MutationCommittedResponse,
    MutationOp,
    MutationRejectedResponse,
    MutationRejectReason,
    NonStateSessionEvent,
    OpenInvokeInfo,
    PreserveEventsOp,
    ResourceChangesEvent,
    ResourceKind,
    ServerFrame,
    ServiceBrokenNotification,
    ServiceMetadataErrorEvent,
    ServiceMetadataResultEvent,
    SessionDebugSnapshotEvent,
    SessionEvent,
    SessionEventFrame,
    SessionSnapshotEvent,
    StateUpdateEvent,
    TrackEntityOp,
    TrackServiceOp,
    UnpreserveEventsOp,
    UntrackEntityOp,
    UntrackServiceOp,
)
from .short_ids import ProvenancedKey, ShortIDAllocator, provenanced_key_for
from .wire_escape import WireEscapeError, escape, unescape

__all__ = [
    # Re-exports for backwards-compatible import paths.
    "ProvenancedKey",
    "provenanced_key_for",
    # Public types defined here:
    "WorkstationState",
    "RPCConnectionRecord",
    "ClientConnectionRecord",
    "TrackedServiceRecord",
    "TrackedEntityRecord",
    "Registration",
    "EntityRegistration",
    "SessionRecord",
    "CallbackRecord",
    "SystemSource",
]


@dataclass(frozen=True)
class SystemSource:
    """Source identifier for workstation-internal service registrations.

    Used as an alternative to :class:`RPCConnectionRecord` on
    :attr:`Registration.source` for services mounted by the
    workstation itself (e.g. stage-7 persistence caps) rather than by
    an external RPC peer. Future workstation-mounted services should
    reuse this shape with a distinct ``kind``.

    The :func:`release_registration` flow special-cases this source —
    there is no per-connection registration list to remove from and no
    ``rpc_connections`` resource to touch — but otherwise treats
    system-sourced registrations like RPC ones (external-name index
    cleanup, tracked-record deref, etc.).

    Attributes:
        kind: Short symbolic tag identifying the system-mounted
            service kind (e.g. ``"persistence"``). Surfaced in the
            services snapshot's debug-only ``system_source_kinds``
            list so the debug UI can label the source. Pick something
            unique per kind; no central registry.
        metadata: Optional auxiliary info attached at registration
            time. Free-form; consumed (today) only by the debug UI.
            Frozen at construction.
    """

    kind: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class Registration:
    """A single external binding to a tracked service.

    Created by :meth:`WorkstationServiceImpl.register_service` for
    RPC-sourced registrations and by stage-7 persistence-cap workers
    for system-sourced ones. Each registration owns a release path
    tied to its source's lifecycle (the dispatch scope's end callback
    for RPC; a workstation-shutdown sweep for system sources);
    :func:`release_registration` is the idempotent cleanup flow.

    Sessions don't create Registrations — they pin services via the
    separate :attr:`TrackedServiceRecord.tracking_sessions` channel.
    A service stays alive while *either* registrations or
    tracking_sessions is non-empty.

    Attributes:
        service_key: The :class:`ServiceKey` this registration is for.
        source: Either the :class:`RPCConnectionRecord` that created
            this registration, or a :class:`SystemSource` if it was
            mounted by the workstation itself.
        external_name: Optional human-readable name. ``None`` for
            id-only registrations.
        released: Idempotency flag for the release flow.
    """

    service_key: ServiceKey
    source: RPCConnectionRecord | SystemSource
    external_name: str | None
    released: bool = False


def describe_registration_source(source: RPCConnectionRecord | SystemSource) -> str:
    """Short human-readable description of a registration's source.

    Used in error messages (e.g. external-name conflict reports) so
    the message format is uniform across RPC peers and
    workstation-internal :class:`SystemSource` mounts.
    """
    if isinstance(source, SystemSource):
        return f"system source {source.kind!r}"
    return f"connection {source.connection_id}"


@dataclass
class EntityRegistration:
    """A single RPC-sourced external binding to a tracked entity.

    Mirror of :class:`Registration` for entities. Created by
    :meth:`WorkstationServiceImpl.copy_entity`; cleaned up by
    :func:`release_entity_registration`. External names are exclusive
    (rejection on conflict at registration time, like services).

    Sessions don't create EntityRegistrations — they pin entities via
    :attr:`TrackedEntityRecord.tracking_sessions`.

    Attributes:
        fxtr_id: The :class:`FxtrID` this registration is for.
        source: The :class:`RPCConnectionRecord` that created this
            registration.
        external_name: Optional human-readable name.
        released: Idempotency flag for the release flow.
    """

    fxtr_id: FxtrID
    source: RPCConnectionRecord
    external_name: str | None
    released: bool = False


@dataclass
class TrackedServiceRecord:
    """Workstation-side bookkeeping for one ``ServiceKey``.

    A record exists while at least one active registration tracks the
    key. The record's :attr:`service_scope` is opened by a worker task
    on :attr:`WorkstationState.background_tg`; the task waits on
    :attr:`shutdown_event` before letting the scope CM drain.

    Attributes:
        service_key: Identity of the tracked service.
        short_id: Workstation short ID (``s{N}``).
        registrations: Active RPC-sourced :class:`Registration`s. The
            record is dropped (and shutdown_event fires) when *both*
            this and :attr:`tracking_sessions` are empty.
        tracking_sessions: UUIDs of sessions that are pinning this
            service via :class:`TrackServiceOp`. Together with
            ``registrations``, controls the record's lifetime.
        bridge_ready: Set by the worker once the service has been
            bridged into :attr:`service_scope`. ``register_service``
            awaits this before returning.
        bridge_error: Populated and ``bridge_ready`` set if the worker
            failed to bridge.
        shutdown_event: Set by the release flow when the last
            registration *and* tracking session drops; the worker
            observes this and lets :attr:`service_scope` drain.
        service_scope: The workstation-owned scope that holds the
            bridge to the registered service. Loadable: the registered
            ``ServiceRef``. Set by the worker before
            :attr:`bridge_ready`. ``None`` until then.
        broken: Set once the grant backing the bridge in
            :attr:`service_scope` is revoked / its RPC connection
            drops (observed via a broken-callback on the bridge's
            keep-alive lease). One-way: a broken service stays *known*
            (the record persists and its short_id keeps resolving while
            tracked/registered) but is unusable — invokes fail and
            sessions holding it receive a
            :class:`ServiceBrokenNotification`. Surfaced on the
            ``services`` resource record as a visible ``broken`` field.
    """

    service_key: ServiceKey
    short_id: str
    registrations: list[Registration] = field(default_factory=list)
    tracking_sessions: set[UUID] = field(default_factory=set)
    bridge_ready: anyio.Event = field(default_factory=anyio.Event)
    bridge_error: BaseException | None = None
    shutdown_event: anyio.Event = field(default_factory=anyio.Event)
    service_scope: FxtrScope | None = None
    broken: bool = False


@dataclass
class TrackedEntityRecord:
    """Workstation-side bookkeeping for one tracked
    ``ProvenancedKey``.

    The entity (in its layered form) is brought into the record's
    own :attr:`scope` at mint time — either via
    ``copy_from_scope`` (RPC ``copy_entity``, invoke / callback
    result delivery) or ``track_from_scope`` (load-result children,
    same-store in-memory transfers). On last-release the scope
    drains via ``force_close``; consumers that retrieved the FxtrID
    via ``get_entity`` keep working past last-release because the
    RPC layer bridges each returned entity's lifetime token across
    the wire — the consumer's connection holds an independent claim
    that outlives the workstation's tracking.

    Each record's scope is an isolated, non-mountable child of
    :attr:`WorkstationState.root_scope`. There is no global
    "entities scope" umbrella — per-record scopes are the only
    container, and abandoning one record never disturbs another
    that shares part of the data subgraph (the v2 arena handles
    overlapping leases via its own refcounting).

    Attributes:
        fxtr_id: Identity of the tracked entity. Layered form: the
            record's :class:`ProvenancedKey` is
            ``provenanced_key_for(fxtr_id)``; the prov half is
            ``fxtr_id._provenance_parent`` (or ``None``).
        short_id: Workstation short ID.
        scope: Per-record :class:`FxtrScope` (isolated, non-mountable,
            child of ``root_scope``) holding the layered FxtrID. Drained
            on last-release.
        registrations: Active RPC-sourced
            :class:`EntityRegistration`s. The record is dropped (and
            its scope drained) when *both* this and
            :attr:`tracking_sessions` are empty.
        tracking_sessions: UUIDs of sessions pinning this entity via
            :class:`TrackEntityOp`.
    """

    fxtr_id: FxtrID
    short_id: str
    scope: FxtrScope
    registrations: list[EntityRegistration] = field(default_factory=list)
    tracking_sessions: set[UUID] = field(default_factory=set)


#: Identity tuple for "the same client" mutator-skip-ahead checks. A
#: ``(client_connection_id, subscription_id)`` pair is treated as one
#: client; anything else (different connection, different subscription
#: on the same connection) is foreign.
MutatorKey: TypeAlias = tuple[UUID, int]


@dataclass
class CallbackRecord:
    """Slot-side handle for a session-owned callback.

    Built end-to-end by :func:`callback_record_worker` during the
    async pre-commit phase of the originating mutation. By the time
    the worker calls ``task_status.started()``, every field is set
    and the underlying service is bridged and resolvable: anyone
    consuming the ref via ``["callback", slot]`` in invoke args gets
    a real :class:`ServiceRef` to substitute, no readiness wait
    needed. If the bridge fails, the worker raises before
    ``started()`` and the whole mutation is rejected with
    :class:`MutationRejectedResponse(reason="callback_setup_failed")`
    — so a ``CallbackRecord`` either exists in
    ``session.open_callbacks`` fully formed, or doesn't exist at
    all.

    Attributes:
        slot: The session-local slot the receive side is bound to.
            Stable for the record's lifetime.
        impl: The mounted :class:`WorkstationInboxImpl`.
            ``close_callback`` and the session-close sweep call
            ``impl.mark_closed()`` to mark the receive side closed
            (and wake its subscription parker tasks for unsubscribe)
            without disturbing external bridges to the send side.
        tracked: The underlying :class:`TrackedServiceRecord`.
        service_key: Convenience copy of ``tracked.service_key``.
        service_short_id: Convenience copy of ``tracked.short_id``.
    """

    slot: int
    impl: WorkstationInboxImpl
    tracked: TrackedServiceRecord
    service_key: ServiceKey
    service_short_id: str


@dataclass(frozen=True)
class _CallbackInvokeTarget:
    """Phase-2 marker for a :class:`CallbackInvokeOp` target.

    Validation can't resolve a callback slot to a :class:`ServiceKey`
    yet — the slot may belong to a ``create_callback`` earlier in the
    same mutation, whose :class:`CallbackRecord` only exists after the
    async pre-commit phase. The marker rides through ``invoke_specs``
    and phase 3 swaps it for the record's ``service_key`` (looked up
    against ``pending_creates ∪ open_callbacks``, exactly like the
    arg-side ``["callback", slot]`` tag).
    """

    callback_slot: int


@dataclass(frozen=True)
class _OpenInvokeRecord:
    """Per-slot description of a pending invoke.

    Held in :attr:`SessionRecord.open_invokes` for the lifetime of
    the in-flight invoke; populated when ``apply_mutation`` spawns
    the worker, removed by the worker's ``finally`` block. The
    ``service_short_id`` is recomputed at snapshot time via
    :class:`ShortIDAllocator` so we don't have to pin the short_id
    string here — but caching it would also be fine. The internal
    `service_key` is stored to remain stable across short_id
    allocator deactivation.
    """

    service_key: ServiceKey
    method_name: str


@dataclass
class SessionRecord:
    """Workstation-side bookkeeping for one persistent session.

    A session is the long-lived collaboration unit between a
    workstation and its clients. Stage 3a populated identity, frozen
    metadata, and an empty state. Stage 3b.1 fleshes out the event
    log and stale-ack bookkeeping (mutation pipeline lives in
    :meth:`WorkstationState.apply_mutation`); 3c.2a adds the refcount
    + event_seq machinery that 3c.2b/3c.3b will use to deliver result
    events into slots.

    Attributes:
        session_id: Server-assigned UUID, stable for the session's
            lifetime.
        metadata: Frozen at :meth:`WorkstationState.create_session`.
            Anything mutable belongs in :attr:`state`.
        created_at: UTC timestamp when the session was created.
        state: Current JSON state (mutation-driven from 3b.1).
        event_seq: Per-session high-water mark for *any* session
            event (state-update or, in 3c, result event). Always
            advances; new events are assigned ``event_seq + 1``.
        last_state_seq: Seq of the latest accepted *state-update*
            event. Used by snapshot replay to identify the most
            recent state. ``0`` until the first accepted mutation.
            For pre-3c.2b sessions this coincides with ``event_seq``;
            in 3c.2b they diverge as result events bump only
            ``event_seq``.
        last_state_ack_seq: ``ack_seq`` carried by the latest accepted
            mutation. Bounds the retention horizon — events with
            ``seq < last_state_ack_seq`` have been dropped from
            :attr:`event_log`.
        slot_counter: High-water mark for slot allocation. ``0``
            until 3c.
        last_mutator: ``(connection_id, subscription_id)`` of the
            latest accepted mutation, used for the same-mutator
            skip-ahead rule. ``None`` if no mutation has succeeded yet.
        event_log: Append-only sequence of session events; events are
            stored in increasing-seq order with no gaps. *Non-state
            events only* — :class:`StateUpdateEvent`s are
            authoritative through ``state`` / ``last_state_seq`` /
            ``last_state_ack_seq`` / ``slot_counter`` /
            ``tracked_entities`` / ``tracked_services`` and broadcast
            in-the-moment via :class:`SessionEventFrame` rather than
            persisted here. After each accepted mutation, events
            with ``seq <= last_state_ack_seq`` (other than those
            moved into :attr:`preserved_events`) are dropped from the
            front (retention horizon). What remains is the unacked
            suffix the next subscriber needs to see.
        tracked_services: ``ServiceKey``s this session is pinning via
            :class:`TrackServiceOp`. *Pin set only* — does not by
            itself drive ``tracking_sessions`` membership; that's
            governed by ``service_refcounts`` (see invariant III in
            ``stage3_sessions.md``). Used for double-track / not-
            tracked validation.
        tracked_entities: Mirror of ``tracked_services`` for entity
            pins.
        entity_refcounts: Total per-entity refcount the session
            holds. Sum of (1 if explicit pin) + (1 per retained
            event ref, in 3c.2b+) + (1 per in-flight delivery
            contribution, in 3c.2b+). The dict's domain is exactly
            the set of entities the session is keeping alive — a
            session is in :attr:`TrackedEntityRecord.tracking_sessions`
            iff its refcount on that entity is non-zero.
        service_refcounts: Mirror of ``entity_refcounts`` for
            services.
        event_refs: For each retained result event in
            :attr:`event_log`, the entity / service refs the event
            materializes. Used to decrement refcounts when the event
            drops past the retention horizon. Empty until 3c.2b
            (no result events exist yet). Stage 8a: an event that has
            been moved from :attr:`event_log` into
            :attr:`preserved_events` keeps its ``event_refs[seq]``
            entry untouched — preserve never decrements refcounts;
            unpreserve and retention prune both decrement the same
            way.
        preserved_events: Result/notification events explicitly held
            past the retention horizon by a :class:`PreserveEventsOp`
            (stage 8a). Keys are seqs; values are the event structs
            themselves, *moved* out of :attr:`event_log` at preserve
            time so :attr:`event_log` stays a contiguous unacked-suffix
            list. Disjoint from :attr:`event_log` (an event is in
            exactly one of the two). Never contains a
            :class:`StateUpdateEvent`. Cleared on session close. The
            corresponding :attr:`event_refs` entries stay in
            :attr:`event_refs` (not duplicated here) so the existing
            decref machinery covers preserve / unpreserve / retention
            uniformly.
        open_callbacks: Receive-side callback records currently open
            on this session, keyed by slot. Populated by
            :class:`CreateCallbackOp`'s commit and removed by
            :class:`CloseCallbackOp` (or session-close sweep). Each
            entry contributes 1 refcount to ``service_refcounts``.
        closed: Set to ``True`` when a :class:`CloseOp` mutation is
            applied. Closed sessions are removed from the registry;
            the flag primarily exists to keep records from being
            mutated after close in long-running edge cases.
    """

    session_id: UUID
    metadata: dict[str, Any]
    created_at: datetime.datetime
    state: dict[str, Any] = field(default_factory=dict)
    event_seq: int = 0
    last_state_seq: int = 0
    last_state_ack_seq: int = 0
    slot_counter: int = 0
    last_mutator: MutatorKey | None = None
    event_log: list[NonStateSessionEvent] = field(default_factory=list)
    tracked_services: set[ServiceKey] = field(default_factory=set)
    tracked_entities: set[ProvenancedKey] = field(default_factory=set)
    entity_refcounts: dict[ProvenancedKey, int] = field(default_factory=dict)
    service_refcounts: dict[ServiceKey, int] = field(default_factory=dict)
    event_refs: dict[int, tuple[set[ProvenancedKey], set[ServiceKey]]] = field(default_factory=dict)
    preserved_events: dict[int, NonStateSessionEvent] = field(default_factory=dict)
    open_callbacks: dict[int, CallbackRecord] = field(default_factory=dict)
    open_invokes: dict[int, _OpenInvokeRecord] = field(default_factory=dict)
    open_loads: dict[int, ProvenancedKey] = field(default_factory=dict)
    closed: bool = False


# --- Connection subscription kinds -----------------------------------
#
# Stage 2 only had resource-list subscriptions. Stage 3a adds
# session and session-list subscriptions. They share the
# per-connection subscription-id space (so a single client can have
# at most one subscription per integer id, regardless of kind), and
# all flow through the same per-connection send queue.


@dataclass(frozen=True)
class ResourceSubscription:
    """Subscription to a stage-2 resource list (rpc_connections /
    services / entities).

    ``debug`` is set from the originating
    :class:`SubscribeRequest.debug`. Stage 4a stores it; per-record
    projection / change-frame filtering land alongside the callsite
    migration to the resource-change buffer.
    """

    resource: ResourceKind
    debug: bool = False


@dataclass(frozen=True)
class SessionSubscription:
    """Subscription to a single session by UUID (stage 3a)."""

    session_id: UUID


@dataclass(frozen=True)
class SessionDebugSubscription:
    """Subscription to a single session's debug stream (stage 4b).

    A separate category from :class:`SessionSubscription` because
    the role and update cadence differ: the main session stream
    carries ack-sequenced events (state updates, slot results),
    whereas the debug stream is a chatty, sequence-less
    full-snapshot push of refcount + ``slot_counter`` state. They
    share teardown timing — closing a session tears down both.
    """

    session_id: UUID


ConnectionSubscription = ResourceSubscription | SessionSubscription | SessionDebugSubscription


#: Default per-connection send buffer. Small enough to catch a slow
#: client quickly; large enough to absorb a flurry of mutations
#: during e.g. setup. See ``stage2_client_listing.md``.
DEFAULT_CLIENT_SEND_BUFFER = 64


@dataclass
class ClientConnectionRecord:
    """Bookkeeping for a single live ``/client`` WebSocket connection.

    Each client connection owns one bounded
    :class:`anyio.abc.ObjectSendStream` that the per-connection write
    loop drains to the WebSocket. Notifiers push :class:`ServerFrame`s
    onto this stream via :meth:`send_nowait`. On overflow we cancel
    the connection's :class:`anyio.CancelScope` and refuse further
    notifications — the per-connection task tree unwinds, the WS
    closes, and the record is removed from
    :attr:`WorkstationState.client_connections`.

    Attributes:
        connection_id: Server-assigned UUID, stable for the connection's
            lifetime.
        peer_addr: ``"host:port"`` of the peer if known.
        connected_at: UTC timestamp when the record was created.
        subscriptions: Active subscriptions for this connection,
            keyed by subscription id. Updated by the read loop's
            subscribe/unsubscribe handlers.
        overflowed: ``True`` once the send buffer has refused a
            frame; subsequent :meth:`send_nowait` calls short-circuit.
    """

    connection_id: UUID
    peer_addr: str | None
    connected_at: datetime.datetime
    _send_stream: anyio.streams.memory.MemoryObjectSendStream[ServerFrame]
    _cancel_scope: anyio.CancelScope
    subscriptions: dict[int, ConnectionSubscription] = field(default_factory=dict)
    overflowed: bool = False
    #: Per-session-subscription set of service short_ids we've already
    #: delivered a :class:`ServiceBrokenNotification` for, keyed by
    #: sub_id. Deduplicates the out-of-band break side channel so a
    #: given subscription is told about a broken service at most once;
    #: a fresh subscription (e.g. on reconnect) starts empty and
    #: re-announces from current state. Cleared per sub_id on
    #: unsubscribe.
    announced_broken: dict[int, set[str]] = field(default_factory=dict)

    def send_nowait(self, frame: ServerFrame) -> bool:
        """Try to enqueue ``frame`` onto the connection's send buffer.

        Returns ``True`` on success, ``False`` if the connection is
        already overflowed or the stream is broken (in either case
        the connection is being torn down). On the first overflow,
        marks ``overflowed=True`` and cancels the connection's
        :attr:`_cancel_scope` so the per-connection task tree unwinds.
        """
        if self.overflowed:
            return False
        try:
            self._send_stream.send_nowait(frame)
            return True
        except (anyio.WouldBlock, anyio.BrokenResourceError, anyio.ClosedResourceError):
            self.overflowed = True
            self._cancel_scope.cancel()
            return False

    def cancel(self) -> None:
        """Trigger this client connection's teardown.

        Cancels the per-connection cancel scope, which unwinds the
        read/write tasks and lets ``add_client_connection``'s CM run
        its cleanup (close the WS, remove from
        ``state.client_connections``). Sync; idempotent.
        """
        self._cancel_scope.cancel()


@dataclass
class RPCConnectionRecord:
    """Bookkeeping for a single live ``/rpc`` WebSocket connection.

    Attributes:
        connection_id: Server-assigned UUID, stable for the connection's
            lifetime.
        peer_addr: ``"host:port"`` of the peer if known.
        connected_at: UTC timestamp when the record was created.
        scope: Per-connection isolated mountable :class:`FxtrScope`.
            Anything mounted on the connection's behalf is loadable
            here.
        name: Optional human-readable name set by the peer via
            ``WorkstationService.name_connection``.
        registrations: The service :class:`Registration`s sourced by
            this connection.
        entity_registrations: The :class:`EntityRegistration`s sourced
            by this connection.
    """

    connection_id: UUID
    peer_addr: str | None
    connected_at: datetime.datetime
    scope: FxtrScope
    name: str | None = None
    registrations: list[Registration] = field(default_factory=list)
    entity_registrations: list[EntityRegistration] = field(default_factory=list)


# --- Resource change buffer (stage 4a) --------------------------------
#
# A :class:`ResourceChangeBuffer` is opened by
# :meth:`WorkstationState.mutation_context` for the duration of a
# top-level mutation. Primitives that mutate a resource record call
# :meth:`WorkstationState.touch` *before* the mutation; the buffer
# captures the pre-image on first touch by reading the current
# registry. At flush time we compare the captured pre-image against
# the post-mutation registry to derive add / update / remove deltas
# and broadcast a single :class:`ResourceChangesEvent` per affected
# resource per subscriber.
#
# Per-subscription debug-flag projection: at flush time, each
# touched (pre, post) pair is projected against the subscription's
# ``debug`` flag (debug-only fields stripped if not debug — see
# :data:`_DEBUG_ONLY_FIELDS`). An ``updated`` whose only diff is in
# debug-only fields is suppressed for non-debug subscribers but
# delivered to debug subscribers.


# Sentinel for "touched but pre-image not yet captured" — distinct
# from "captured a None pre-image (record didn't exist before this
# mutation)". A plain ``object()`` is fine for identity comparisons.
_NOT_CAPTURED: Any = object()


@dataclass
class _TouchedRecord:
    """Per-(resource, key) bookkeeping inside a
    :class:`ResourceChangeBuffer`.

    ``pre_image`` is the snapshot of the record as it existed before
    the buffer was opened (None if the record didn't exist), captured
    lazily on first touch. ``_NOT_CAPTURED`` means the touch hasn't
    materialized a pre-image read yet.
    """

    pre_image: dict[str, Any] | None = field(default=_NOT_CAPTURED)


@dataclass
class ResourceChangeBuffer:
    """Per-mutation accumulator of resource records that were touched.

    Used by :meth:`WorkstationState.mutation_context` to coalesce a
    burst of registry changes (e.g. an :meth:`apply_mutation` call
    that touches services + entities + sessions) into a single
    :class:`ResourceChangesEvent` per resource per subscriber.
    """

    per_resource: dict[ResourceKind, dict[str, _TouchedRecord]] = field(default_factory=dict)

    def mark_touched(
        self,
        state: WorkstationState,
        resource: ResourceKind,
        key: str,
    ) -> None:
        """Record that the record identified by ``(resource, key)``
        is being mutated. Captures the pre-image on first touch by
        reading the current registry; subsequent touches in the same
        buffer are no-ops (the pre-image stays anchored to the buffer
        opening).

        Must be called *before* the actual registry mutation so the
        pre-image reflects pre-mutation state.
        """
        per_key = self.per_resource.setdefault(resource, {})
        touched = per_key.get(key)
        if touched is None:
            touched = _TouchedRecord()
            per_key[key] = touched
        if touched.pre_image is _NOT_CAPTURED:
            touched.pre_image = _build_record_for(state, resource, key)


@dataclass(frozen=True)
class _PendingMutation:
    """One queued :class:`MutateSessionRequest` waiting for the session
    worker to pick it up.

    Mutation handling is asynchronous and per-session: a connection's
    read loop validates the subscription locally, packages the
    request into a ``_PendingMutation``, and ``send_nowait``s it onto
    the session's mutation queue. The session's worker task drains
    the queue sequentially, calling
    :meth:`WorkstationState.apply_mutation` on each entry. Mutations
    on different sessions run concurrently on their respective
    workers; mutations on the same session are serialized.
    """

    mutator_connection: ClientConnectionRecord
    mutator_subscription_id: int
    request_id: int
    ack_seq: int
    update_id: int
    patch: list[dict[str, Any]]
    ops: list[MutationOp]


#: Bounded buffer for the per-session mutation queue. Sized to
#: absorb a small flurry without the read loop blocking; over this,
#: the request is rejected with
#: :class:`MutationRejectedResponse(reason="session_busy")` and the
#: client is expected to retry.
SESSION_MUTATION_QUEUE_SIZE = 64


@dataclass
class WorkstationState:
    """In-memory workstation registries.

    Attributes:
        root_scope: The workstation's primary isolated mountable
            :class:`FxtrScope`.
        background_tg: Long-lived task group for workstation worker
            tasks (e.g. per-service-record scope owners). Owned and
            entered by :class:`WorkstationServer`.
        rpc_connections: Live ``/rpc`` connections, by UUID.
        client_connections: Live ``/client`` connections, by UUID.
            See :class:`ClientConnectionRecord`.
        service_records: Tracked services, by :class:`ServiceKey`.
        service_external_names: Exclusive index from external-name to
            the active service :class:`Registration` that bound it.
            ``register_service`` rejects duplicates.
        entity_records: Tracked entities, by :class:`FxtrID`.
        entity_external_names: Exclusive index from external-name to
            the active :class:`EntityRegistration` that bound it.
            ``copy_entity`` rejects duplicates.
        short_ids: Run-lifetime short-ID allocator.

    **Concurrency.** All registry mutations are sync and rely on GIL
    atomicity for safety: a single read-loop / dispatch task runs to
    completion between awaits, so two tasks can't interleave inside
    the sync windows that read-then-write these dicts. The async
    methods on this class never await between the read and the write
    in those windows.
    """

    root_scope: FxtrScope
    background_tg: anyio.abc.TaskGroup
    rpc_connections: dict[UUID, RPCConnectionRecord] = field(default_factory=dict)
    client_connections: dict[UUID, ClientConnectionRecord] = field(default_factory=dict)
    service_records: dict[ServiceKey, TrackedServiceRecord] = field(default_factory=dict)
    service_external_names: dict[str, Registration] = field(default_factory=dict)
    entity_records: dict[ProvenancedKey, TrackedEntityRecord] = field(default_factory=dict)
    entity_external_names: dict[str, EntityRegistration] = field(default_factory=dict)
    sessions: dict[UUID, SessionRecord] = field(default_factory=dict)
    #: Per-session inbound mutation queue (send half). Created at
    #: ``create_session`` time alongside the session record; popped +
    #: closed when the session is dropped from the registry (CloseOp
    #: commit). Mutations sent to a closed / missing entry surface as
    #: :class:`MutationRejectedResponse(reason="session_gone")`. A
    #: full queue surfaces as ``reason="session_busy"``.
    session_mutation_queues: dict[
        UUID, anyio.streams.memory.MemoryObjectSendStream["_PendingMutation"]
    ] = field(default_factory=dict)
    short_ids: ShortIDAllocator = field(default_factory=ShortIDAllocator)

    # Active resource-change buffer, set by :meth:`mutation_context`.
    # ``None`` outside a mutation. Stage 4a: infrastructure only — no
    # production callsites use this yet.
    _current_buffer: ResourceChangeBuffer | None = field(default=None)

    # --- Subscription indexes (stage 4b cleanup) --------------------
    #
    # State-level reverse indexes from the lookup field
    # (``ResourceKind`` / session ``UUID``) to the per-connection
    # subscriptions. Each value dict is keyed by
    # ``(connection_id, sub_id)`` and maps to the owning
    # :class:`ClientConnectionRecord`. The indexes are maintained
    # alongside ``ClientConnectionRecord.subscriptions`` via
    # :meth:`_register_subscription` / :meth:`_unregister_subscription`
    # so the emit paths can iterate just the matching subscriptions
    # without scanning every connection × every subscription kind.
    _resource_subs: dict[ResourceKind, dict[tuple[UUID, int], ClientConnectionRecord]] = field(
        default_factory=dict
    )
    _session_subs: dict[UUID, dict[tuple[UUID, int], ClientConnectionRecord]] = field(
        default_factory=dict
    )
    _session_debug_subs: dict[UUID, dict[tuple[UUID, int], ClientConnectionRecord]] = field(
        default_factory=dict
    )

    # --- Resource-change buffer (stage 4a) --------------------------

    @contextmanager
    def mutation_context(self) -> Generator[None]:
        """Open a top-level mutation context.

        The first (outermost) entry allocates a fresh
        :class:`ResourceChangeBuffer`, makes it the active buffer for
        nested calls to :meth:`touch`, and on a clean exit flushes
        accumulated changes as :class:`ResourceChangesEvent` frames.
        Nested entries are no-ops — they participate in the parent's
        buffer.

        Exception during the body: the buffer is dropped without
        flushing. Rationale: an exception means the mutation is
        unwinding state, and pre-flush partial state isn't a coherent
        thing to broadcast. (Today's callers — register_service,
        copy_entity, apply_mutation — already handle their own
        rollback so no broadcast is the right answer; if a future
        caller wants partial-flush semantics, we can revisit.)

        Sync, must run on the event loop thread between awaits.
        """
        if self._current_buffer is not None:
            yield
            return
        buffer = ResourceChangeBuffer()
        self._current_buffer = buffer
        try:
            yield
        except BaseException:
            self._current_buffer = None
            raise
        self._current_buffer = None
        self._flush_buffer(buffer)

    def touch(self, resource: ResourceKind, key: str) -> None:
        """Record that a resource record is being mutated under the
        active :meth:`mutation_context`.

        Must be called *before* the registry mutation so the
        pre-image captured by the buffer reflects pre-mutation state.
        Idempotent within a single buffer — only the first touch per
        ``(resource, key)`` pair captures a pre-image; subsequent
        touches are no-ops.

        Raises if no mutation context is active.
        """
        buffer = self._current_buffer
        if buffer is None:
            raise RuntimeError(
                "WorkstationState.touch() called outside an active mutation_context()"
            )
        buffer.mark_touched(self, resource, key)

    # --- Subscription registration (stage 4b cleanup) ---------------

    def _register_subscription(
        self,
        conn: ClientConnectionRecord,
        sub_id: int,
        sub: ConnectionSubscription,
    ) -> None:
        """Add ``sub`` to ``conn.subscriptions[sub_id]`` and to the
        matching state-level reverse index.

        The caller is responsible for rejecting duplicate sub_ids
        before calling (the per-connection ``subscriptions`` dict
        would otherwise silently overwrite). Sync."""
        conn.subscriptions[sub_id] = sub
        composite_key = (conn.connection_id, sub_id)
        if isinstance(sub, ResourceSubscription):
            self._resource_subs.setdefault(sub.resource, {})[composite_key] = conn
        elif isinstance(sub, SessionSubscription):
            self._session_subs.setdefault(sub.session_id, {})[composite_key] = conn
        elif isinstance(sub, SessionDebugSubscription):
            self._session_debug_subs.setdefault(sub.session_id, {})[composite_key] = conn
        else:  # pragma: no cover
            raise AssertionError(f"unhandled subscription kind: {type(sub).__name__}")

    def _unregister_subscription(
        self,
        conn: ClientConnectionRecord,
        sub_id: int,
    ) -> ConnectionSubscription | None:
        """Pop ``sub_id`` from ``conn.subscriptions`` and remove it
        from the matching state-level reverse index. Returns the
        removed subscription, or ``None`` if no such subscription
        existed (idempotent for callers like the connection-drop
        sweep). Sync."""
        sub = conn.subscriptions.pop(sub_id, None)
        if sub is None:
            return None
        composite_key = (conn.connection_id, sub_id)
        if isinstance(sub, ResourceSubscription):
            entries = self._resource_subs.get(sub.resource)
            if entries is not None:
                entries.pop(composite_key, None)
                if not entries:
                    del self._resource_subs[sub.resource]
        elif isinstance(sub, SessionSubscription):
            entries = self._session_subs.get(sub.session_id)
            if entries is not None:
                entries.pop(composite_key, None)
                if not entries:
                    del self._session_subs[sub.session_id]
            conn.announced_broken.pop(sub_id, None)
        elif isinstance(sub, SessionDebugSubscription):
            entries = self._session_debug_subs.get(sub.session_id)
            if entries is not None:
                entries.pop(composite_key, None)
                if not entries:
                    del self._session_debug_subs[sub.session_id]
        else:  # pragma: no cover
            raise AssertionError(f"unhandled subscription kind: {type(sub).__name__}")
        return sub

    def _flush_buffer(self, buffer: ResourceChangeBuffer) -> None:
        """Compute deltas + dispatch :class:`ResourceChangesEvent`
        frames to active resource subscribers. Sync.

        For each resource, walks the touched-key map and reads the
        post-mutation registry via :func:`_build_record_for`. Per
        subscription, the (pre, post) pair is projected against the
        subscription's ``debug`` flag (debug-only fields stripped
        for non-debug subscribers, kept for debug subscribers) and
        classified into ``added`` / ``updated`` / ``removed``:

        - pre None, post not None → added.
        - pre not None, post None → removed.
        - pre == post → skip (touched but no change).
        - projected pre == projected post → skip for *this*
          subscriber (the change was entirely in debug-only fields
          that this subscriber doesn't see).
        - else → updated.

        A frame with all three lists empty after filtering is
        suppressed entirely.
        """
        for resource, per_key in buffer.per_resource.items():
            # Pre-compute pre/post pairs once per touched key. The
            # per-subscription loop below only differs by debug
            # projection of the same pair.
            pairs: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = []
            for key, touched in per_key.items():
                pre = touched.pre_image
                # ``_TouchedRecord`` only enters ``per_key`` via
                # :meth:`ResourceChangeBuffer.mark_touched`, which
                # always captures a pre-image on first touch. An
                # un-captured entry means someone bypassed that path
                # and the buffer is in an inconsistent state.
                assert (
                    pre is not _NOT_CAPTURED
                ), f"buffer entry for {resource}:{key} has no captured pre-image"
                post = _build_record_for(self, resource, key)
                if pre is None and post is None:
                    continue
                if pre is not None and post is not None and pre == post:
                    continue
                pairs.append((key, pre, post))
            if not pairs:
                continue

            for (_, sub_id), conn in list(self._resource_subs.get(resource, {}).items()):
                sub = conn.subscriptions.get(sub_id)
                # Defensive: index entries should always have a
                # matching ``conn.subscriptions`` entry. If not, the
                # subscription was torn down concurrently — skip.
                if not isinstance(sub, ResourceSubscription):
                    continue  # pragma: no cover
                added: list[dict[str, Any]] = []
                updated: list[dict[str, Any]] = []
                removed: list[str] = []
                for key, pre, post in pairs:
                    if pre is None:
                        assert post is not None
                        added.append(_project_record(resource, post, sub.debug))
                    elif post is None:
                        removed.append(key)
                    else:
                        proj_pre = _project_record(resource, pre, sub.debug)
                        proj_post = _project_record(resource, post, sub.debug)
                        if proj_pre == proj_post:
                            # Change was entirely in debug-only
                            # fields invisible to this subscriber.
                            continue
                        updated.append(proj_post)
                if not (added or updated or removed):
                    continue
                conn.send_nowait(
                    ResourceChangesEvent(
                        subscription_id=sub_id,
                        added=added,
                        updated=updated,
                        removed=removed,
                    )
                )

    @asynccontextmanager
    async def add_rpc_connection(
        self, peer_addr: str | None
    ) -> AsyncGenerator[RPCConnectionRecord]:
        """Track a new RPC connection for the duration of the context.

        Opens a per-connection ``isolated=True`` scope
        with ``include=[self.root_scope]``, constructs a record
        carrying that scope, and yields. On exit any still-active
        registrations sourced from this connection are released, the
        scope drains, and the record is removed from
        :attr:`rpc_connections`.
        """
        connection_id = uuid4()
        async with FxtrScope(
            isolated=True,
            include=[self.root_scope],
            name=f"rpc-conn/{connection_id}",
        ) as conn_scope:
            record = RPCConnectionRecord(
                connection_id=connection_id,
                peer_addr=peer_addr,
                connected_at=datetime.datetime.now(datetime.timezone.utc),
                scope=conn_scope,
            )
            with self.mutation_context():
                self.touch("rpc_connections", str(record.connection_id))
                self.rpc_connections[record.connection_id] = record
            try:
                yield record
            finally:
                # Defensive sweep in case the normal cleanup path fails.
                # Healthy teardown auto-releases each registration via
                # the end-callback on its register-call's dispatch
                # scope; if anything is still on ``record.registrations``
                # / ``record.entity_registrations`` by the time we get
                # here, log it and clean it up so we don't leak — and
                # so we notice a missed teardown path rather than
                # silently masking it.
                with self.mutation_context():
                    for registration in list(record.registrations):
                        _log.warning(
                            "rpc connection %s: service registration %r was "
                            "not released by its dispatch-scope end-callback "
                            "before connection teardown; releasing now",
                            record.connection_id,
                            registration,
                        )
                        release_registration(self, registration)
                    for entity_reg in list(record.entity_registrations):
                        _log.warning(
                            "rpc connection %s: entity registration %r was "
                            "not released by its dispatch-scope end-callback "
                            "before connection teardown; releasing now",
                            record.connection_id,
                            entity_reg,
                        )
                        release_entity_registration(self, entity_reg)
                    self.touch("rpc_connections", str(record.connection_id))
                    self.rpc_connections.pop(record.connection_id, None)

    @asynccontextmanager
    async def add_client_connection(
        self,
        peer_addr: str | None,
        *,
        send_buffer_size: int = DEFAULT_CLIENT_SEND_BUFFER,
    ) -> AsyncGenerator[
        tuple[
            ClientConnectionRecord,
            anyio.streams.memory.MemoryObjectReceiveStream[ServerFrame],
        ]
    ]:
        """Track a new ``/client`` connection for the duration of the context.

        Constructs a :class:`ClientConnectionRecord` carrying a
        bounded :class:`MemoryObjectSendStream` and a
        :class:`anyio.CancelScope`. Yields ``(record, receive_stream)``:
        the caller is responsible for running the read/write loops
        inside the cancel scope (so an overflow-triggered cancel
        unwinds them). On exit the record is removed from
        :attr:`client_connections` and both stream halves are closed.
        """
        connection_id = uuid4()
        send_stream, receive_stream = anyio.create_memory_object_stream[ServerFrame](
            send_buffer_size
        )
        async with send_stream, receive_stream:
            with anyio.CancelScope() as cancel_scope:
                record = ClientConnectionRecord(
                    connection_id=connection_id,
                    peer_addr=peer_addr,
                    connected_at=datetime.datetime.now(datetime.timezone.utc),
                    _send_stream=send_stream,
                    _cancel_scope=cancel_scope,
                )
                self.client_connections[connection_id] = record
                try:
                    yield record, receive_stream
                finally:
                    # Sweep any subscriptions still held by this
                    # connection so the state-level reverse indexes
                    # don't keep dangling references after the
                    # connection drops. ``_unregister_subscription``
                    # is idempotent.
                    for sub_id in list(record.subscriptions.keys()):
                        self._unregister_subscription(record, sub_id)
                    self.client_connections.pop(connection_id, None)

    def build_snapshot(self, resource: ResourceKind) -> list[dict[str, Any]]:
        """Build a full snapshot for ``resource``. See the per-builder
        docstrings for the per-record shapes. Sync."""
        if resource == "rpc_connections":
            return _build_rpc_connections_snapshot(self)
        if resource == "services":
            return _build_services_snapshot(self)
        if resource == "entities":
            return _build_entities_snapshot(self)
        if resource == "sessions":
            return _build_sessions_snapshot(self)
        raise ValueError(f"unknown resource: {resource!r}")  # pragma: no cover

    # --- Session lifecycle (stage 3a) -------------------------------

    def create_session(self, metadata: dict[str, Any]) -> SessionRecord:
        """Create a new :class:`SessionRecord` and emit a
        ``sessions`` resource delta to subscribers. Sync — caller is
        on the event loop thread.

        Also creates the session's bounded mutation queue and spawns
        a :func:`session_worker` task on :attr:`background_tg` to
        drain it. Mutations submitted via
        :class:`MutateSessionRequest` flow through the queue; the
        worker calls :meth:`apply_mutation` for each.
        """
        record = SessionRecord(
            session_id=uuid4(),
            metadata=metadata,
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
        send, receive = anyio.create_memory_object_stream[_PendingMutation](
            max_buffer_size=SESSION_MUTATION_QUEUE_SIZE
        )
        with self.mutation_context():
            self.touch("sessions", str(record.session_id))
            self.sessions[record.session_id] = record
            self.session_mutation_queues[record.session_id] = send
        self.background_tg.start_soon(session_worker, self, record.session_id, receive)
        return record

    def build_session_snapshot(
        self,
        subscription_id: int,
        session_id: UUID,
    ) -> SessionSnapshotEvent:
        """Build the initial snapshot for a session subscription.

        ``last_state`` is reconstructed from the record's
        authoritative state fields (``state`` / ``last_state_seq`` /
        ``last_state_ack_seq`` / ``slot_counter`` / explicit-pin
        sets); for a session with no committed mutations it's a
        synthetic ``seq=0 ack_seq=0 state={} slot_counter=0`` event.
        State_updates are not stored in ``event_log`` — they're
        always reconstructed here.

        Two non-state event buckets, both sorted by seq:

        - ``preserved_events``: from ``record.preserved_events`` (the
          sidecar of events held past the retention horizon by an
          earlier :class:`PreserveEventsOp`). Each has
          ``seq <= last_state.ack_seq``.
        - ``subsequent_events``: from ``record.event_log`` (the
          unacked suffix). Each has ``seq > last_state.ack_seq``.

        Raises :class:`KeyError` if the session does not exist.
        """
        record = self.sessions[session_id]
        last_state = StateUpdateEvent(
            seq=record.last_state_seq,
            ack_seq=record.last_state_ack_seq,
            state=record.state,
            tracked_entities=sorted(
                self.short_ids.short_id_for_entity(key.to_layered_fxtr_id())
                for key in record.tracked_entities
            ),
            tracked_services=sorted(
                self.short_ids.short_id_for_service(sk) for sk in record.tracked_services
            ),
            slot_counter=record.slot_counter,
        )
        # Invariant: the retention prune in ``apply_mutation`` drops
        # any non-preserved event with ``seq <= last_state_ack_seq``
        # from ``event_log``; ``event_log`` itself is a contiguous
        # unacked-suffix list of *non-state* events. Explicitly
        # preserved events (each with ``seq <= last_state_ack_seq``)
        # live in ``record.preserved_events``.
        assert all(e.seq > record.last_state_ack_seq for e in record.event_log)
        preserved_in_order = [
            record.preserved_events[seq] for seq in sorted(record.preserved_events)
        ]
        return SessionSnapshotEvent(
            subscription_id=subscription_id,
            session_id=str(session_id),
            last_state=last_state,
            preserved_events=preserved_in_order,
            subsequent_events=list(record.event_log),
        )

    def announce_broken_services_to_subscription(
        self,
        conn: ClientConnectionRecord,
        sub_id: int,
        session_id: UUID,
    ) -> None:
        """Trigger 3: after a fresh session subscription's snapshot is
        sent, announce every service the session currently holds that
        is *already* broken. Re-derives brokenness from current state
        (rather than replaying a log), so a subscriber that arrives
        after the break — or reconnects — still learns about it. Sync;
        no-op if the session is gone."""
        session = self.sessions.get(session_id)
        if session is None:
            return
        for service_key in session.service_refcounts:
            tracked = self.service_records.get(service_key)
            if tracked is not None and tracked.broken:
                _announce_broken_service_to_subscription(conn, sub_id, tracked.short_id)

    # --- Mutation pipeline (stage 3b.1+) ----------------------------

    async def apply_mutation(
        self,
        session_id: UUID,
        *,
        mutator_connection: ClientConnectionRecord,
        mutator_subscription_id: int,
        request_id: int,
        ack_seq: int,
        update_id: int,
        patch: list[dict[str, Any]],
        ops: list[MutationOp],
    ) -> None:
        """Process a :class:`MutateSessionRequest`. Four explicit
        phases:

        1. **Validate ops** (sync, no state changes). Stale-ack /
           conflict checks; op-validation against working sets
           (slot monotonicity, unknown short_ids, double-track,
           etc.); JSON-Patch dry-run (stores ``new_state`` for
           commit). ``InvokeOp`` args aren't walked here — that
           would need a different ``lookup_callback`` than the
           commit phase, since callbacks-being-created don't yet
           exist.
        2. **Async pre-commit** (rollbackable). For each
           ``CreateCallbackOp``, ``await background_tg.start(
           callback_record_worker, …)`` — the worker mounts the
           impl, brings up the underlying tracked record, awaits
           its bridge, and signals readiness. A bridge failure
           releases any already-built callbacks and rejects the
           whole mutation as
           :class:`MutationRejectedResponse(reason="callback_setup_failed")`.
        3. **Validate invoke args** (sync). Now that every
           callback this mutation requested is fully built, walk
           ``InvokeOp`` args through ``unescape`` with a real
           ``ServiceRef``-returning ``lookup_callback`` (looking
           up against ``pending_creates`` ∪
           ``record.open_callbacks``). The decoded args are
           handed to phase 4. A failure here releases pre-commit
           creates and rejects the mutation as
           ``validation_failed``.
        4. **Commit** (sync, always commits, in
           :meth:`mutation_context`). Apply patch → tracker adds
           → attach pre-built callbacks → claim arg refs + spawn
           invoke / load workers → tracker removes (incl.
           ``close_callback``) → retention prune → close drain →
           build / append / broadcast :class:`StateUpdateEvent`
           → mutator response → drop the session if closing.

        Mutations on the same session are serialized by the per-
        session :func:`session_worker` task that owns the session's
        mutation queue; mutations on different sessions run
        concurrently on their respective workers.

        On success: applies the patch, processes the ops, appends the
        resulting :class:`SessionEvent` (a :class:`StateUpdateEvent`,
        or a :class:`ClosedSessionEvent` if a :class:`CloseOp` was
        present), advances seqs, prunes the log to the new retention
        horizon, broadcasts to foreign session subscribers, and
        sends a :class:`MutationCommittedResponse` to the mutator. If
        the mutation closed the session, the session is dropped from
        the registry and all subscriptions to it are torn down (the
        mutator's subscription silently — the ack is the close
        signal — and foreign subscriptions after their close event
        is enqueued).

        On failure: sends a single :class:`MutationRejectedResponse`
        with a typed :data:`MutationRejectReason` and a diagnostic
        message to ``mutator_connection``. No state mutated, no
        events appended, no slots consumed.

        Invariants:
        - ``mutator_subscription_id`` must reference an active
          :class:`SessionSubscription` on ``mutator_connection`` for
          ``session_id``. The handler validates this before calling
          us; we re-check defensively.
        - A :class:`CloseOp` must be the last op in ``ops``.
        """

        def _reject(reason: MutationRejectReason, message: str = "") -> None:
            mutator_connection.send_nowait(
                MutationRejectedResponse(request_id=request_id, reason=reason, message=message)
            )

        # ====================================================
        # Phase 1: Validate (sync, no state changes).
        # ====================================================
        record = self.sessions.get(session_id)
        if record is None or record.closed:
            _reject("session_gone", f"no session with id {session_id}")
            return

        # Defensive: subscription on this connection must point at
        # this session.
        sub = mutator_connection.subscriptions.get(mutator_subscription_id)
        if not isinstance(sub, SessionSubscription) or sub.session_id != session_id:
            _reject(
                "validation_failed",
                f"subscription {mutator_subscription_id} is not an active "
                f"subscription to session {session_id} on this connection",
            )
            return

        mutator_key: MutatorKey = (
            mutator_connection.connection_id,
            mutator_subscription_id,
        )

        # --- Stale-ack check ---------------------------------------
        if ack_seq < record.last_state_ack_seq:
            _reject(
                "stale_ack",
                f"ack_seq {ack_seq} is below the session's retention horizon "
                f"({record.last_state_ack_seq}); ack_seq must be monotonic "
                "non-decreasing",
            )
            return
        if ack_seq < record.last_state_seq and record.last_mutator != mutator_key:
            _reject(
                "conflict",
                "stale ack: another client has updated the session since "
                f"ack_seq {ack_seq} (last state seq is {record.last_state_seq}). "
                "Re-fetch the latest state and retry.",
            )
            return

        # --- Op validation (dry-run with working sets) -------------
        # Validate each op against a working view of the session's
        # tracker sets. Each op may add or remove a planned entry;
        # subsequent ops see the planned state. If any op fails, the
        # entire mutation is rejected (no side effects).
        # After validation, we compute the *net* diff vs. the session's
        # current registrations and apply that — so e.g. "track A,
        # untrack A" produces no real-world change rather than a
        # broken add+remove pair.
        is_closing = False
        existing_entities: set[ProvenancedKey] = set(record.tracked_entities)
        existing_services: set[ServiceKey] = set(record.tracked_services)
        working_entities = set(existing_entities)
        working_services = set(existing_services)
        # Running max for slots claimed by load/invoke ops. Initialized
        # to the session's current ``slot_counter`` so the first op
        # must pick a slot strictly greater. Each load/invoke op
        # bumps it. This single counter enforces both
        # "monotonic-across-mutations" and "unique-within-this-mutation"
        # in one rule: each new slot must be strictly greater than
        # everything claimed before, full stop.
        max_slot_claimed = record.slot_counter
        # Each entry is (slot, fxtr_id, silent). 8c: ``silent=True``
        # entries come from :class:`LoadEntitySilentOp` and tell the
        # worker to omit the payload from the result event.
        load_specs: list[tuple[int, FxtrID, bool]] = []
        # Each invoke spec stores the *raw* (still-escaped) ``op.args`` and
        # ``op.kwargs``; phase 3 walks them with a real ``ServiceRef``-returning
        # ``lookup_callback`` once pre-commit has built any callbacks
        # this mutation requested. The target is a resolved
        # :class:`ServiceKey` for :class:`InvokeOp` or a
        # :class:`_CallbackInvokeTarget` marker for
        # :class:`CallbackInvokeOp` (resolved in phase 3, same reason
        # as the args).
        invoke_specs: list[
            tuple[int, ServiceKey | _CallbackInvokeTarget, str, list[Any], dict[str, Any]]
        ] = []
        # Metadata-query specs collected during validation. Each
        # entry's ``slot`` claims a fresh slot like load / invoke; the
        # commit phase resolves the metadata sync and broadcasts the
        # result event.
        entity_metadata_specs: list[tuple[int, FxtrID]] = []
        service_metadata_specs: list[tuple[int, ServiceKey]] = []
        # Working view of the session's open callbacks — used to
        # validate ``CloseCallbackOp``s as we walk ops. A close is
        # legal iff its slot is currently open at that point in the
        # walk (existing slot, or one being created earlier in this
        # mutation). Slot monotonicity prevents re-creating a closed
        # slot, so the working set is purely append-or-remove with
        # no replays.
        working_callback_slots: set[int] = set(record.open_callbacks)
        # Each ``CreateCallbackOp`` / ``CloseCallbackOp``'s slot,
        # collected literally in op order. Unlike tracker ops we
        # don't compute a net diff — a within-mutation
        # ``[create N, close N]`` actually creates and closes the
        # callback (briefly), so an ``[create N, invoke uses N,
        # close N]`` sequence works as written. The wasted work of
        # a literal create-then-immediate-close is acceptable; it's
        # a niche case.
        callback_create_slots: list[int] = []
        callback_close_slots: list[int] = []
        # Working set of preserved-event seqs (stage 8a). Threaded
        # through ``PreserveEventsOp`` / ``UnpreserveEventsOp`` so
        # later ops in the same mutation see the running effect of
        # earlier ones (e.g. ``[Unpreserve(B), Preserve(A)]`` is fine
        # even when B was previously preserved). After validation,
        # the diff against ``record.preserved_events`` keys yields
        # ``to_preserve`` / ``to_unpreserve`` for the commit phase.
        existing_preserved: set[int] = set(record.preserved_events.keys())
        working_preserved: set[int] = set(existing_preserved)

        for op_index, op in enumerate(ops):
            if isinstance(op, CloseOp):
                # CloseOp must be the *last* op in the mutation. Ops
                # before it run normally — useful for sequences like
                # "release tracker pins / unpreserve preserved events
                # / clear user state, then close" that terminating a
                # session with active state needs. Anything after
                # CloseOp would execute against an already-draining
                # session record, so we forbid that.
                #
                # Work-launching ops (load / invoke / metadata /
                # create_callback) bundled with CloseOp aren't a
                # validation error, but their result events never
                # land (the session record is gone by delivery time;
                # the deliver_* helpers short-circuit on
                # session-gone). It's a footgun, not a correctness
                # hole.
                if op_index != len(ops) - 1:
                    _reject(
                        "validation_failed",
                        "CloseOp must be the last op in the mutation",
                    )
                    return
                is_closing = True
            elif isinstance(op, TrackEntityOp):
                fxtr_id = self.short_ids.lookup_active_entity(op.short_id)
                if fxtr_id is None:
                    _reject(
                        "validation_failed",
                        f"unknown or inactive entity short id {op.short_id!r}",
                    )
                    return
                entity_key = provenanced_key_for(fxtr_id)
                if entity_key not in self.entity_records:
                    _reject(
                        "validation_failed",
                        f"unknown or inactive entity short id {op.short_id!r}",
                    )
                    return
                if entity_key in working_entities:
                    _reject(
                        "validation_failed",
                        f"session already tracks entity {op.short_id!r}",
                    )
                    return
                working_entities.add(entity_key)
            elif isinstance(op, UntrackEntityOp):
                fxtr_id = self.short_ids.lookup_active_entity(op.short_id)
                if fxtr_id is None:
                    _reject(
                        "validation_failed",
                        f"unknown or inactive entity short id {op.short_id!r}",
                    )
                    return
                entity_key = provenanced_key_for(fxtr_id)
                if entity_key not in working_entities:
                    _reject(
                        "validation_failed",
                        f"session does not track entity {op.short_id!r}",
                    )
                    return
                working_entities.discard(entity_key)
            elif isinstance(op, TrackServiceOp):
                service_key = self.short_ids.lookup_active_service(op.short_id)
                if service_key is None or service_key not in self.service_records:
                    _reject(
                        "validation_failed",
                        f"unknown or inactive service short id {op.short_id!r}",
                    )
                    return
                if service_key in working_services:
                    _reject(
                        "validation_failed",
                        f"session already tracks service {op.short_id!r}",
                    )
                    return
                working_services.add(service_key)
            elif isinstance(op, UntrackServiceOp):
                service_key = self.short_ids.lookup_active_service(op.short_id)
                if service_key is None:
                    _reject(
                        "validation_failed",
                        f"unknown or inactive service short id {op.short_id!r}",
                    )
                    return
                if service_key not in working_services:
                    _reject(
                        "validation_failed",
                        f"session does not track service {op.short_id!r}",
                    )
                    return
                working_services.discard(service_key)
            elif isinstance(op, (LoadEntityOp, LoadEntitySilentOp)):
                fxtr_id = self.short_ids.lookup_active_entity(op.short_id)
                if fxtr_id is None or provenanced_key_for(fxtr_id) not in self.entity_records:
                    _reject(
                        "validation_failed",
                        f"unknown or inactive entity short id {op.short_id!r}",
                    )
                    return
                if op.slot <= max_slot_claimed:
                    _reject(
                        "validation_failed",
                        f"slot {op.slot} must be strictly greater than "
                        f"{max_slot_claimed} (the highest slot claimed before "
                        "this op, including this mutation's earlier ops and "
                        "the session's prior slot_counter)",
                    )
                    return
                max_slot_claimed = op.slot
                load_specs.append((op.slot, fxtr_id, isinstance(op, LoadEntitySilentOp)))
            elif isinstance(op, InvokeOp):
                service_key = self.short_ids.lookup_active_service(op.service_short_id)
                if service_key is None or service_key not in self.service_records:
                    _reject(
                        "validation_failed",
                        f"unknown or inactive service short id {op.service_short_id!r}",
                    )
                    return
                if op.slot <= max_slot_claimed:
                    _reject(
                        "validation_failed",
                        f"slot {op.slot} must be strictly greater than "
                        f"{max_slot_claimed} (the highest slot claimed before "
                        "this op, including this mutation's earlier ops and "
                        "the session's prior slot_counter)",
                    )
                    return

                max_slot_claimed = op.slot
                # Defer arg decoding to phase 3 (after pre-commit
                # has built any callbacks this mutation requested).
                # Storing the raw escaped args / kwargs here lets the
                # single phase-3 walk use a real
                # ``ServiceRef``-returning ``lookup_callback`` without
                # a separate structural-validation pass.
                invoke_specs.append(
                    (
                        op.slot,
                        service_key,
                        op.method_name,
                        list(op.args),
                        dict(op.kwargs),
                    )
                )
            elif isinstance(op, CallbackInvokeOp):
                # Target is one of the session's own callbacks. The
                # working set covers both pre-existing open callbacks
                # and ones created earlier in this mutation's ops list
                # (minus any closed earlier in the list), so ordering
                # within the mutation behaves like every other op.
                if op.callback_slot not in working_callback_slots:
                    _reject(
                        "validation_failed",
                        f"slot {op.callback_slot} does not have an open callback",
                    )
                    return
                if op.slot <= max_slot_claimed:
                    _reject(
                        "validation_failed",
                        f"slot {op.slot} must be strictly greater than "
                        f"{max_slot_claimed} (the highest slot claimed before "
                        "this op, including this mutation's earlier ops and "
                        "the session's prior slot_counter)",
                    )
                    return
                max_slot_claimed = op.slot
                # Target ServiceKey resolution deferred to phase 3 —
                # for a same-mutation ``create_callback`` the record
                # doesn't exist until pre-commit runs.
                invoke_specs.append(
                    (
                        op.slot,
                        _CallbackInvokeTarget(op.callback_slot),
                        op.method_name,
                        list(op.args),
                        dict(op.kwargs),
                    )
                )
            elif isinstance(op, GetEntityMetadataOp):
                fxtr_id = self.short_ids.lookup_active_entity(op.short_id)
                if fxtr_id is None or provenanced_key_for(fxtr_id) not in self.entity_records:
                    _reject(
                        "validation_failed",
                        f"unknown or inactive entity short id {op.short_id!r}",
                    )
                    return
                if op.slot <= max_slot_claimed:
                    _reject(
                        "validation_failed",
                        f"slot {op.slot} must be strictly greater than "
                        f"{max_slot_claimed} (the highest slot claimed before "
                        "this op, including this mutation's earlier ops and "
                        "the session's prior slot_counter)",
                    )
                    return
                max_slot_claimed = op.slot
                entity_metadata_specs.append((op.slot, fxtr_id))
            elif isinstance(op, GetServiceMetadataOp):
                service_key = self.short_ids.lookup_active_service(op.short_id)
                if service_key is None or service_key not in self.service_records:
                    _reject(
                        "validation_failed",
                        f"unknown or inactive service short id {op.short_id!r}",
                    )
                    return
                if op.slot <= max_slot_claimed:
                    _reject(
                        "validation_failed",
                        f"slot {op.slot} must be strictly greater than "
                        f"{max_slot_claimed} (the highest slot claimed before "
                        "this op, including this mutation's earlier ops and "
                        "the session's prior slot_counter)",
                    )
                    return
                max_slot_claimed = op.slot
                service_metadata_specs.append((op.slot, service_key))
            elif isinstance(op, CreateCallbackOp):
                if op.slot <= max_slot_claimed:
                    _reject(
                        "validation_failed",
                        f"slot {op.slot} must be strictly greater than "
                        f"{max_slot_claimed} (the highest slot claimed before "
                        "this op, including this mutation's earlier ops and "
                        "the session's prior slot_counter)",
                    )
                    return
                # By construction unreachable (slot > max_slot_claimed
                # implies slot is fresh).
                assert op.slot not in working_callback_slots
                max_slot_claimed = op.slot
                working_callback_slots.add(op.slot)
                callback_create_slots.append(op.slot)
            elif isinstance(op, CloseCallbackOp):
                if op.slot not in working_callback_slots:
                    _reject(
                        "validation_failed",
                        f"slot {op.slot} does not have an open callback",
                    )
                    return
                working_callback_slots.discard(op.slot)
                callback_close_slots.append(op.slot)
            elif isinstance(op, PreserveEventsOp):
                if len(op.seqs) != len(set(op.seqs)):
                    _reject(
                        "validation_failed",
                        f"PreserveEventsOp has duplicate seqs in {op.seqs!r}",
                    )
                    return
                for seq in op.seqs:
                    if seq in working_preserved:
                        _reject(
                            "validation_failed",
                            f"event seq {seq} is already preserved",
                        )
                        return
                    # ``event_log`` is non-state by construction (state_updates
                    # are authoritative through the record's
                    # ``last_state_*`` fields and never appended), so a
                    # match here is automatically a non-state event —
                    # no isinstance(StateUpdateEvent) check needed.
                    target = next((e for e in record.event_log if e.seq == seq), None)
                    if target is None:
                        _reject(
                            "validation_failed",
                            f"cannot preserve event seq {seq}: not in the session's" " event log",
                        )
                        return
                    if seq > ack_seq:
                        # Still in the unacked suffix — preserving
                        # would be a no-op (retention won't drop it
                        # this mutation anyway).
                        _reject(
                            "validation_failed",
                            f"cannot preserve event seq {seq}: it is not being "
                            f"acknowledged by this mutation (ack_seq={ack_seq})",
                        )
                        return
                    working_preserved.add(seq)
            elif isinstance(op, UnpreserveEventsOp):
                if len(op.seqs) != len(set(op.seqs)):
                    _reject(
                        "validation_failed",
                        f"UnpreserveEventsOp has duplicate seqs in {op.seqs!r}",
                    )
                    return
                for seq in op.seqs:
                    if seq not in working_preserved:
                        _reject(
                            "validation_failed",
                            f"cannot unpreserve event seq {seq}: not currently" " preserved",
                        )
                        return
                    working_preserved.discard(seq)
            else:  # pragma: no cover
                _reject("validation_failed", f"unhandled op kind: {type(op).__name__}")
                return

        # Net diff for the apply phase.
        net_entity_adds = working_entities - existing_entities
        net_entity_removes = existing_entities - working_entities
        net_service_adds = working_services - existing_services
        net_service_removes = existing_services - working_services
        # Stage 8a: preserve / unpreserve net diff. ``to_preserve`` are
        # seqs to move from ``event_log`` into the sidecar at commit;
        # ``to_unpreserve`` are seqs to drop from the sidecar (with
        # ``event_refs`` decref). Within-mutation ``[Preserve(A),
        # Unpreserve(A)]`` produces empty diffs — A was never in the
        # original set, was added then removed, so the working set
        # ends unchanged. A still rides ``event_log`` retention.
        to_preserve = working_preserved - existing_preserved
        to_unpreserve = existing_preserved - working_preserved

        # --- JSON Patch dry-run (still validation) -----------------
        # Apply with ``in_place=False`` so we hold a fresh
        # post-state copy for the commit phase. Patch failures are
        # validation errors — the commit phase is async-precommit-
        # only-failable from here on, so any failure that could
        # surface must be caught now.
        try:
            new_state = jsonpatch.apply_patch(record.state, patch, in_place=False)
        except jsonpatch.JsonPatchException as e:
            _reject("validation_failed", f"json-patch failed: {e}")
            return
        except jsonpatch.JsonPointerException as e:
            _reject("validation_failed", f"json-pointer failed: {e}")
            return
        except (TypeError, ValueError) as e:
            _reject("validation_failed", f"json-patch malformed: {e}")
            return

        # ====================================================
        # Phase 2: Async pre-commit (rollbackable).
        # ====================================================
        # Bring up every ``CreateCallbackOp``'s :class:`CallbackRecord`
        # by spawning a ``callback_record_worker`` per slot via
        # ``task_group.start(...)``. ``start`` returns the
        # :class:`CallbackRecord` once the worker has signalled
        # readiness (``task_status.started(cb)``); a bridge failure
        # raises before that signal fires. On any failure we roll
        # back any pre-built records (release their tracked records,
        # which signals their workers' shutdown_event), reject the
        # whole mutation, and don't enter commit.
        pending_creates: dict[int, CallbackRecord] = {}
        if callback_create_slots:
            try:
                for slot in callback_create_slots:
                    cb = await self.background_tg.start(
                        callback_record_worker, self, session_id, slot
                    )
                    pending_creates[slot] = cb
            except Exception as exc:
                with self.mutation_context():
                    for cb in pending_creates.values():
                        maybe_release_service_record(self, cb.tracked)
                _reject("callback_setup_failed", repr(exc))
                return

        # ====================================================
        # Phase 3: Validate invoke args (sync, real ServiceRefs).
        # ====================================================
        # Pre-commit just brought every requested callback up, so
        # ``lookup_callback`` here returns a real
        # :class:`ServiceRef` for either an existing
        # ``record.open_callbacks`` entry or a freshly-built
        # ``pending_creates`` entry. Failure rolls back pre-commit
        # work and rejects the mutation.
        def _lookup_callback(slot: int) -> ServiceRef[Any]:
            cb = pending_creates.get(slot)
            if cb is None:
                cb = record.open_callbacks.get(slot)
            if cb is None:
                raise WireEscapeError(f"no open callback at slot {slot} in this session")
            return ServiceRef(
                handle=cb.service_key.handle,
                target_type=UnknownService,
                service_type_id=cb.service_key.type_id,
            )

        decoded_invokes: list[tuple[int, ServiceKey, str, list[Any], dict[str, Any]]] = []
        try:
            for slot, target, method_name, raw_args, raw_kwargs in invoke_specs:
                if isinstance(target, _CallbackInvokeTarget):
                    # Same lookup the arg-side ``["callback", slot]``
                    # tag uses. Validation checked the slot against the
                    # working set and close pops only happen at commit,
                    # so this can't miss.
                    cb = pending_creates.get(target.callback_slot)
                    if cb is None:
                        cb = record.open_callbacks.get(target.callback_slot)
                    assert cb is not None
                    service_key = cb.service_key
                else:
                    service_key = target
                resolved_args = [
                    unescape(
                        a,
                        lookup_entity=self._lookup_active_entity_or_raise,
                        lookup_service=self._lookup_active_service_or_raise,
                        lookup_callback=_lookup_callback,
                    )
                    for a in raw_args
                ]
                resolved_kwargs = {
                    k: unescape(
                        v,
                        lookup_entity=self._lookup_active_entity_or_raise,
                        lookup_service=self._lookup_active_service_or_raise,
                        lookup_callback=_lookup_callback,
                    )
                    for k, v in raw_kwargs.items()
                }
                decoded_invokes.append(
                    (slot, service_key, method_name, resolved_args, resolved_kwargs)
                )
        except WireEscapeError as exc:
            with self.mutation_context():
                for cb in pending_creates.values():
                    maybe_release_service_record(self, cb.tracked)
            _reject("validation_failed", f"invoke args invalid: {exc}")
            return

        # ====================================================
        # Phase 4: Commit (sync, always commits).
        # ====================================================
        with self.mutation_context():
            # --- a. Apply patch ------------------------------------
            new_seq = record.event_seq + 1
            record.state = new_state
            record.event_seq = new_seq
            record.last_state_seq = new_seq
            record.last_state_ack_seq = ack_seq
            record.last_mutator = mutator_key

            # --- b. Tracker adds (before retention) ----------------
            # Adds first so explicit ``track_*`` ops in this mutation
            # land before refcounts decrement — a mutation that both
            # acks past a result event and explicitly pins one of
            # its refs preserves the ref (incref → 2, then retention
            # decref → 1, no 1→0 transition).
            for entity_key in net_entity_adds:
                _add_session_entity_tracker(self, record, entity_key)
            for service_key in net_service_adds:
                _add_session_service_tracker(self, record, service_key)

            # --- c. Attach pre-built callbacks ---------------------
            for slot, cb_record in pending_creates.items():
                record.open_callbacks[slot] = cb_record
                _incref_session_service(self, record, cb_record.service_key)

            # --- d. Claim arg refs + spawn invoke / load workers --
            # Arg refs are incref'd against the calling session as
            # in-flight delivery contributions — dropped in each
            # worker's ``finally`` block.
            slot_counter_changed = max_slot_claimed != record.slot_counter
            record.slot_counter = max_slot_claimed
            if slot_counter_changed:
                _emit_session_debug_snapshot(self, record)

            for slot, fxtr_id, silent in load_specs:
                load_key = provenanced_key_for(fxtr_id)
                arg_e: set[ProvenancedKey] = {load_key}
                arg_s: set[ServiceKey] = set()
                # 5e.1: surface the pending load on the debug snapshot.
                # Set *before* the arg incref so the incref's debug
                # emit picks up the new open_loads entry; the
                # corresponding pop fires in ``load_worker``'s finally
                # block (and re-emits then).
                record.open_loads[slot] = load_key
                # Op-validation guarantees the record exists for
                # ``load_key``. The incref below will fail loudly if
                # not — that signals a bug elsewhere (concurrent
                # release across our pre-commit await window).
                _incref_session_entity(self, record, load_key)
                self.background_tg.start_soon(
                    load_worker, self, session_id, slot, fxtr_id, arg_e, arg_s, silent
                )

            for (
                slot,
                service_key,
                method_name,
                resolved_args,
                resolved_kwargs,
            ) in decoded_invokes:
                arg_e = set()
                arg_s = {service_key}
                # ``_walk_refs`` recurses through dicts and lists, so
                # passing the combined args + kwargs values picks up
                # refs from both sides for the in-flight refcount
                # bookkeeping below.
                arg_fids, arg_srefs = _walk_refs([resolved_args, resolved_kwargs])
                for fid in arg_fids:
                    arg_e.add(provenanced_key_for(fid))
                for sref in arg_srefs:
                    arg_s.add(ServiceKey(handle=sref.handle, type_id=sref.service_type_id))
                # 5e.1: surface the pending invoke on the debug snapshot.
                # Set *before* the arg increfs (same reason as load).
                record.open_invokes[slot] = _OpenInvokeRecord(
                    service_key=service_key, method_name=method_name
                )
                # Op-validation (via the unescape lookup callback)
                # guarantees a record exists for every entity-typed
                # arg's :class:`ProvenancedKey`. Increfs below will
                # fail loudly if not (signal of a bug elsewhere).
                for fid in arg_fids:
                    _incref_session_entity(self, record, provenanced_key_for(fid))
                for sk in arg_s:
                    _incref_session_service(self, record, sk)
                self.background_tg.start_soon(
                    invoke_worker,
                    self,
                    session_id,
                    slot,
                    service_key,
                    method_name,
                    resolved_args,
                    resolved_kwargs,
                    arg_e,
                    arg_s,
                )

            # --- e. Tracker removes (incl. close_callback) ---------
            # Run *after* the in-flight invoke arg-incref so a
            # within-mutation ``[invoke uses S, untrack_service S]``
            # pair keeps S alive across the untrack via the
            # in-flight contribution. Callback closes are similar:
            # any invoke that used the callback as an arg has
            # already claimed an in-flight delivery contribution
            # via the arg-walk above, so the close's decref of the
            # open-callback contribution doesn't release the
            # underlying record while the invoke is in flight.
            for slot in callback_close_slots:
                cb_record = record.open_callbacks.pop(slot)
                # Sync + idempotent: flips ``_closed`` and wakes the
                # impl's subscription parker tasks, which do the async
                # unsubscribe work on ``background_tg``.
                cb_record.impl.mark_closed()
                _decref_session_service(self, record, cb_record.service_key)
            for entity_key in net_entity_removes:
                _remove_session_entity_tracker(self, record, entity_key)
            for service_key in net_service_removes:
                _remove_session_service_tracker(self, record, service_key)

            # --- e.5a Preserve moves (before retention prune) -----
            # Move each newly-preserved event out of ``event_log``
            # into ``preserved_events``. Validation guaranteed each
            # seq exists in ``event_log`` and is not a state_update;
            # ``event_refs[seq]`` stays untouched (refcounts persist
            # via the sidecar).
            if to_preserve:
                kept: list[NonStateSessionEvent] = []
                for evt in record.event_log:
                    if evt.seq in to_preserve:
                        record.preserved_events[evt.seq] = evt
                    else:
                        kept.append(evt)
                record.event_log = kept

            # --- e.5b Unpreserve drops (before retention prune) ---
            # Pop each unpreserved seq from the sidecar and decref
            # its ``event_refs[seq]`` contributions. The event
            # itself is past the horizon by definition (only
            # past-horizon events live in ``preserved_events``), so
            # it's discarded — matches the semantics of "you can
            # only unpreserve events you have already preserved".
            if to_unpreserve:
                for seq in to_unpreserve:
                    record.preserved_events.pop(seq, None)
                    ref_entry = record.event_refs.pop(seq, None)
                    if ref_entry is not None:
                        refs_e, refs_s = ref_entry
                        for ref_key in refs_e:
                            _decref_session_entity(self, record, ref_key)
                        for ref_sk in refs_s:
                            _decref_session_service(self, record, ref_sk)

            # If preserve / unpreserve changed the preserved set
            # without triggering refcount transitions (e.g. a
            # CallbackEvent with a string payload — no FxtrID
            # children, so ``event_refs[seq]`` is empty), the
            # existing refcount-driven debug-snapshot emits don't
            # fire. Emit one explicitly so debug subscribers see the
            # updated ``preserved_events`` list — without this, the
            # UI's preserved chips wouldn't refresh until the next
            # refcount change or a manual resubscribe.
            if to_preserve or to_unpreserve:
                _emit_session_debug_snapshot(self, record)

            # --- Retention horizon advance ------------------------
            # Contiguous-prefix drop on ``event_log``. Preserved
            # events were already moved out by (e.5a) above, so the
            # contiguous-prefix invariant holds: every event with
            # ``seq <= ack_seq`` still in ``event_log`` is a
            # to-drop event.
            drop_count = 0
            for e in record.event_log:
                if e.seq <= ack_seq:
                    drop_count += 1
                else:
                    break
            if drop_count:
                for dropped in record.event_log[:drop_count]:
                    ref_entry = record.event_refs.pop(dropped.seq, None)
                    if ref_entry is not None:
                        refs_e, refs_s = ref_entry
                        for ref_key in refs_e:
                            _decref_session_entity(self, record, ref_key)
                        for ref_sk in refs_s:
                            _decref_session_service(self, record, ref_sk)
                del record.event_log[:drop_count]

            # --- Close drain (before event construction) ----------
            # Empty out the session's pin sets so the
            # StateUpdateEvent's ``tracked_entities`` /
            # ``tracked_services`` snapshots are empty on the
            # terminal close event.
            if is_closing:
                record.closed = True
                _close_session_drain_trackers(self, record)
                self.touch("sessions", str(session_id))

            # --- Build state-update event -------------------------
            # Authoritative through the record's ``last_state_*`` /
            # ``state`` / ``slot_counter`` fields (already updated
            # above) and broadcast in-the-moment via ``SessionEventFrame``.
            # *Not* appended to ``event_log`` — ``event_log`` is for
            # non-state events only (post-3-post-2 invariant).
            # ``build_session_snapshot`` reconstructs the synthetic
            # ``last_state`` from the record on demand.
            tracked_entities_snap = sorted(
                self.short_ids.short_id_for_entity(key.to_layered_fxtr_id())
                for key in record.tracked_entities
            )
            tracked_services_snap = sorted(
                self.short_ids.short_id_for_service(sk) for sk in record.tracked_services
            )
            event = StateUpdateEvent(
                seq=new_seq,
                ack_seq=ack_seq,
                state=new_state,
                closed=is_closing,
                tracked_entities=tracked_entities_snap,
                tracked_services=tracked_services_snap,
                slot_counter=record.slot_counter,
            )

            # --- Broadcast + mutator response ---------------------
            for (_, sub_id), conn in list(self._session_subs.get(session_id, {}).items()):
                if conn is mutator_connection and sub_id == mutator_subscription_id:
                    continue
                conn.send_nowait(SessionEventFrame(subscription_id=sub_id, event=event))
                if is_closing:
                    self._unregister_subscription(conn, sub_id)
            if is_closing:
                for (_, sub_id), conn in list(self._session_debug_subs.get(session_id, {}).items()):
                    self._unregister_subscription(conn, sub_id)
                # Silently tear down the mutator's own session
                # subscription (the mutation_committed response is
                # the close signal).
                self._unregister_subscription(mutator_connection, mutator_subscription_id)
            mutator_connection.send_nowait(
                MutationCommittedResponse(
                    request_id=request_id,
                    subscription_id=mutator_subscription_id,
                    seq=new_seq,
                    update_id=update_id,
                )
            )

            # --- Metadata query ops (6c, sync resolution) ---------
            # Resolved sync after the state-update broadcast so the
            # result events are sequenced cleanly and visible to
            # every subscriber via ``_append_session_event``. Each
            # op consumed a slot during validation; the slot_counter
            # already reflects ``max_slot_claimed``. Refs surfaced
            # in the metadata payload are incref'd against this
            # session and tracked in ``event_refs[seq]`` so the
            # retention horizon decrements them when the metadata
            # event ages out — same pattern as load / invoke
            # results.
            for slot, fxtr_id in entity_metadata_specs:
                ent_key = provenanced_key_for(fxtr_id)
                ent_record = self.entity_records.get(ent_key)
                if ent_record is None:
                    # Race: record disappeared between op-validation
                    # and the commit phase. Surface as an error
                    # event rather than rejecting the whole mutation
                    # (the slot was already counted into
                    # slot_counter; the state update has been
                    # broadcast).
                    record.event_seq += 1
                    err_event: SessionEvent = EntityMetadataErrorEvent(
                        seq=record.event_seq,
                        slot=slot,
                        message=(
                            f"entity record for {fxtr_id.id_string()} disappeared "
                            "between op-validation and commit"
                        ),
                    )
                    _append_session_event(
                        self, record, err_event, in_flight_e=set(), in_flight_s=set()
                    )
                    continue
                metadata, ref_keys = _compute_entity_metadata(self, ent_record)
                in_flight_e: set[ProvenancedKey] = set()
                seen_keys: set[ProvenancedKey] = set()
                for k in ref_keys:
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    _incref_session_entity(self, record, k)
                    in_flight_e.add(k)
                record.event_seq += 1
                ok_event: SessionEvent = EntityMetadataResultEvent(
                    seq=record.event_seq, slot=slot, metadata=metadata
                )
                _append_session_event(
                    self, record, ok_event, in_flight_e=in_flight_e, in_flight_s=set()
                )
            for slot, service_key in service_metadata_specs:
                svc_record = self.service_records.get(service_key)
                if svc_record is None:
                    record.event_seq += 1
                    svc_err: SessionEvent = ServiceMetadataErrorEvent(
                        seq=record.event_seq,
                        slot=slot,
                        message=(
                            f"service record for {service_key} disappeared "
                            "between op-validation and commit"
                        ),
                    )
                    _append_session_event(
                        self, record, svc_err, in_flight_e=set(), in_flight_s=set()
                    )
                    continue
                svc_metadata, svc_ref_keys = _compute_service_metadata(svc_record)
                svc_in_flight_s: set[ServiceKey] = set()
                for sk in svc_ref_keys:
                    _incref_session_service(self, record, sk)
                    svc_in_flight_s.add(sk)
                record.event_seq += 1
                svc_ok: SessionEvent = ServiceMetadataResultEvent(
                    seq=record.event_seq, slot=slot, metadata=svc_metadata
                )
                _append_session_event(
                    self,
                    record,
                    svc_ok,
                    in_flight_e=set(),
                    in_flight_s=svc_in_flight_s,
                )

            # --- Session registry teardown if closing -------------
            if is_closing:
                del self.sessions[session_id]
                send_stream = self.session_mutation_queues.pop(session_id, None)
                if send_stream is not None:
                    send_stream.close()

    # --- Wire-side lookup helpers (3c.3b) ---------------------------
    #
    # Adapters from short_id strings to the corresponding ``FxtrID`` /
    # ``ServiceRef`` for use as ``unescape`` callbacks during op
    # validation. Raise :class:`WireEscapeError` so the validator can
    # surface a single "invoke args invalid: …" error rather than
    # leaking a different exception type.

    def _lookup_active_entity_or_raise(self, short_id: str) -> FxtrID:
        fxtr_id = self.short_ids.lookup_active_entity(short_id)
        if fxtr_id is None:
            raise WireEscapeError(f"unknown or inactive entity short id {short_id!r}")
        if provenanced_key_for(fxtr_id) not in self.entity_records:
            raise WireEscapeError(f"unknown or inactive entity short id {short_id!r}")
        return fxtr_id

    def _lookup_active_service_or_raise(self, short_id: str) -> ServiceRef[Any]:
        service_key = self.short_ids.lookup_active_service(short_id)
        if service_key is None or service_key not in self.service_records:
            raise WireEscapeError(f"unknown or inactive service short id {short_id!r}")
        return ServiceRef(
            handle=service_key.handle,
            target_type=UnknownService,
            service_type_id=service_key.type_id,
        )


# --- Snapshot builders ------------------------------------------------
#
# Sync. Read the registries directly. See ``stage2_client_listing.md``
# for the per-record shape.


#: Per-resource fields that are stripped from records sent to non-debug
#: subscribers. The debug-only set is also what controls the
#: "non-debug-visible projection" suppression rule in
#: :meth:`WorkstationState._flush_buffer`: an ``updated`` event whose
#: only differences are confined to these fields is suppressed for
#: non-debug subscribers but still delivered to debug subscribers.
#:
#: ``sessions`` has no debug-only fields today — the debug flag is
#: accepted for forward compatibility but is a no-op for that
#: resource (per-session debug data is exposed via the dedicated
#: ``subscribe_session_debug`` op landing in stage 4b).
_DEBUG_ONLY_FIELDS: dict[ResourceKind, frozenset[str]] = {
    "rpc_connections": frozenset(
        {"service_registration_short_ids", "entity_registration_short_ids"}
    ),
    "services": frozenset(
        {"registering_connection_ids", "system_source_kinds", "pinned_by_sessions"}
    ),
    "entities": frozenset({"registering_connection_ids", "pinned_by_sessions"}),
    "sessions": frozenset(),
}


def _project_record(
    resource: ResourceKind,
    record: dict[str, Any],
    debug: bool,
) -> dict[str, Any]:
    """Return ``record`` with debug-only fields stripped if ``debug``
    is False; otherwise return ``record`` unchanged.

    Debug subscribers see the full record produced by the snapshot
    builders; non-debug subscribers see the production-facing subset
    (per :data:`_DEBUG_ONLY_FIELDS`)."""
    if debug:
        return record
    debug_fields = _DEBUG_ONLY_FIELDS.get(resource, frozenset())
    if not debug_fields:
        return record
    return {k: v for k, v in record.items() if k not in debug_fields}


def _build_rpc_connection_record(
    state: WorkstationState, record: RPCConnectionRecord
) -> dict[str, Any]:
    """Per-record dict for one live ``/rpc`` connection. Lists the
    short-IDs of services / entities sourced by that connection."""
    return {
        "connection_id": str(record.connection_id),
        "name": record.name,
        "peer_addr": record.peer_addr,
        "connected_at": record.connected_at.isoformat(),
        "service_registration_short_ids": [
            state.short_ids.short_id_for_service(reg.service_key) for reg in record.registrations
        ],
        "entity_registration_short_ids": [
            state.short_ids.short_id_for_entity(reg.fxtr_id) for reg in record.entity_registrations
        ],
    }


def _build_rpc_connections_snapshot(state: WorkstationState) -> list[dict[str, Any]]:
    """Full snapshot of the live ``/rpc`` connections list."""
    return [
        _build_rpc_connection_record(state, record) for record in state.rpc_connections.values()
    ]


def _build_service_record(state: WorkstationState, tracked: TrackedServiceRecord) -> dict[str, Any]:
    """Per-record dict for one :class:`TrackedServiceRecord`.
    ``pinned_by_sessions`` lists the UUIDs of sessions tracking the
    service via :class:`TrackServiceOp` (3d).

    Service registrations have two source kinds (since stage 7a):

    - RPC peers: surfaced via ``registering_connection_ids``.
    - Workstation-internal :class:`SystemSource` mounts (e.g. stage-7
      persistence caps): surfaced via ``system_source_kinds``.

    Both lists are debug-only — see :data:`_DEBUG_ONLY_FIELDS`.
    """
    # ``state`` is unused here — kept so all three per-record builders
    # share one signature, which lets :func:`_build_record_for`
    # dispatch through them uniformly.
    del state
    external_names = [
        reg.external_name for reg in tracked.registrations if reg.external_name is not None
    ]
    registering_connection_ids = [
        str(reg.source.connection_id)
        for reg in tracked.registrations
        if isinstance(reg.source, RPCConnectionRecord)
    ]
    system_source_kinds = sorted(
        {reg.source.kind for reg in tracked.registrations if isinstance(reg.source, SystemSource)}
    )
    return {
        "short_id": tracked.short_id,
        "service_type_id": tracked.service_key.type_id.id_string(),
        "external_names": external_names,
        "registering_connection_ids": registering_connection_ids,
        "system_source_kinds": system_source_kinds,
        "pinned_by_sessions": [str(sid) for sid in tracked.tracking_sessions],
        # Visible (non-debug) third state: ``True`` once the bridge's
        # grant was revoked / its RPC connection dropped. List/debug
        # tables render this (e.g. strikethrough); the flip emits a
        # normal ``resource_changes`` delta via the change buffer.
        "broken": tracked.broken,
    }


def _build_services_snapshot(state: WorkstationState) -> list[dict[str, Any]]:
    """Full snapshot of all :class:`TrackedServiceRecord`s."""
    return [_build_service_record(state, tracked) for tracked in state.service_records.values()]


def _build_entity_record(state: WorkstationState, tracked: TrackedEntityRecord) -> dict[str, Any]:
    """Per-record dict for one :class:`TrackedEntityRecord`.
    ``pinned_by_sessions`` lists the UUIDs of sessions tracking the
    entity via :class:`TrackEntityOp` (3d)."""
    # ``state`` is unused here — kept so all three per-record builders
    # share one signature, which lets :func:`_build_record_for`
    # dispatch through them uniformly.
    del state
    external_names = [
        reg.external_name for reg in tracked.registrations if reg.external_name is not None
    ]
    registering_connection_ids = [str(reg.source.connection_id) for reg in tracked.registrations]
    return {
        "short_id": tracked.short_id,
        "fxtr_id": tracked.fxtr_id.id_string(),
        "external_names": external_names,
        "registering_connection_ids": registering_connection_ids,
        "pinned_by_sessions": [str(sid) for sid in tracked.tracking_sessions],
    }


def _build_entities_snapshot(state: WorkstationState) -> list[dict[str, Any]]:
    """Full snapshot of all :class:`TrackedEntityRecord`s."""
    return [_build_entity_record(state, tracked) for tracked in state.entity_records.values()]


def _build_record_for(
    state: WorkstationState,
    resource: ResourceKind,
    key: str,
) -> dict[str, Any] | None:
    """Build the snapshot dict for a single resource record by its
    record key (``connection_id`` for ``rpc_connections``, short ID
    for ``services`` / ``entities``). Returns ``None`` if the record
    does not currently exist in the registry.

    Used by :class:`ResourceChangeBuffer` to capture pre/post images
    around a mutation. Intentionally tolerant of ill-formed keys
    (e.g. a non-UUID ``connection_id``): returns ``None`` rather
    than raising, so a flush over a partially-touched key set never
    crashes the mutation pipeline.
    """
    if resource == "rpc_connections":
        try:
            uuid_key = UUID(key)
        except ValueError:
            return None  # pragma: no cover
        record = state.rpc_connections.get(uuid_key)
        if record is None:
            return None
        return _build_rpc_connection_record(state, record)
    if resource == "services":
        service_key = state.short_ids.lookup_active_service(key)
        if service_key is None:
            return None
        tracked = state.service_records.get(service_key)
        if tracked is None:
            return None  # pragma: no cover
        return _build_service_record(state, tracked)
    if resource == "entities":
        fxtr_id = state.short_ids.lookup_active_entity(key)
        if fxtr_id is None:
            return None
        tracked = state.entity_records.get(provenanced_key_for(fxtr_id))
        if tracked is None:
            return None  # pragma: no cover
        return _build_entity_record(state, tracked)
    if resource == "sessions":
        try:
            session_uuid = UUID(key)
        except ValueError:
            return None  # pragma: no cover
        record = state.sessions.get(session_uuid)
        if record is None:
            return None
        return _build_session_record(state, record)
    raise ValueError(f"unknown resource: {resource!r}")  # pragma: no cover


def _build_session_record(state: WorkstationState, record: SessionRecord) -> dict[str, Any]:
    """Per-record dict for one :class:`SessionRecord` as it appears
    in the ``sessions`` resource (stage 4a).

    Lifecycle-only — ``session_id`` / ``metadata`` / ``created_at``.
    State-progression fields (``last_state_seq``, ``last_state_ack_seq``,
    ``slot_counter``, refcounts, etc.) are deliberately excluded; they
    tick on every mutation and would make this resource noisy for
    subscribers that only want session lifecycle. Live state is
    available via ``subscribe_session`` (event log) and (in 4b)
    ``subscribe_session_debug`` (refcounts + slot_counter).
    """
    # ``state`` is unused here — kept so all four per-record builders
    # share one signature, which lets :func:`_build_record_for`
    # dispatch through them uniformly.
    del state
    return {
        "session_id": str(record.session_id),
        "metadata": record.metadata,
        "created_at": record.created_at.isoformat(),
    }


def _build_sessions_snapshot(state: WorkstationState) -> list[dict[str, Any]]:
    """Full snapshot of the live sessions list (stage 4a)."""
    return [_build_session_record(state, record) for record in state.sessions.values()]


def release_registration(state: WorkstationState, registration: Registration) -> None:
    """Idempotent: drop a registration from all bookkeeping.

    Synchronous — runs under the event-loop's single thread, so the
    list-mutation operations are atomic by GIL ordering. Callable
    from sync contexts (e.g. the dispatch scope's
    ``register_end_callback``) as well as from async dispatch methods.

    Opens its own :meth:`WorkstationState.mutation_context` if none
    is active (the end-callback path), or joins an active parent
    context (the ``apply_mutation`` untrack-op / close-drain paths).

    Steps:

    1. Mark the registration as released; bail if already so.
    2. Remove it from the corresponding
       :class:`TrackedServiceRecord.registrations`.
    3. If the record now has no registrations *and* no
       :attr:`TrackedServiceRecord.tracking_sessions`, drop it from
       :attr:`WorkstationState.service_records`, deactivate the short
       ID, and set the record's :attr:`shutdown_event` so the worker
       task lets :attr:`service_scope` drain.
    4. For RPC sources, remove from the source connection's
       :attr:`RPCConnectionRecord.registrations`. System-sourced
       registrations skip this step (no per-connection list).
    5. Clear the external-name index entry if it still points at us.
    """
    if registration.released:
        return
    registration.released = True

    with state.mutation_context():
        # Invariant maintained by ``register_service``: a non-released
        # ``Registration`` is in both ``tracked.registrations`` and
        # ``record.registrations`` exactly once. The ``released``
        # short-circuit above means the explicit removes can never be
        # repeated.
        tracked = state.service_records.get(registration.service_key)
        if tracked is not None:
            state.touch("services", tracked.short_id)
            tracked.registrations.remove(registration)
            maybe_release_service_record(state, tracked)
        if isinstance(registration.source, RPCConnectionRecord):
            state.touch("rpc_connections", str(registration.source.connection_id))
            registration.source.registrations.remove(registration)
        # System-sourced registrations have no per-connection list to
        # update; the source ``SystemSource`` is just a label.
        if registration.external_name is not None:
            if state.service_external_names.get(registration.external_name) is registration:
                del state.service_external_names[registration.external_name]


def release_entity_registration(state: WorkstationState, registration: EntityRegistration) -> None:
    """Idempotent: drop an entity registration from all bookkeeping.

    Mirror of :func:`release_registration` for entities. Same atomicity
    guarantees (sync, GIL-atomic). Steps:

    1. Mark released; bail if already so.
    2. Remove from the :class:`TrackedEntityRecord`. If the record's
       registrations list is now empty, pop it from
       :attr:`WorkstationState.entity_records`, deactivate the short
       ID, and drain the record's per-record :attr:`FxtrScope` via
       ``force_close``. Consumers that retrieved the FxtrID via
       ``get_entity`` keep working: the RPC layer bridges the
       entity's lifetime token across the wire at call return time,
       so the consumer's connection holds an independent claim.
    3. Remove from the source RPC connection's
       :attr:`RPCConnectionRecord.entity_registrations`.
    4. Clear the external-name index entry if it still points at us.

    The record is dropped (and its per-record scope drained) only
    when *both* the registrations list and
    :attr:`TrackedEntityRecord.tracking_sessions` are empty.
    """
    if registration.released:
        return
    registration.released = True

    with state.mutation_context():
        tracked = state.entity_records.get(provenanced_key_for(registration.fxtr_id))
        if tracked is not None:
            state.touch("entities", tracked.short_id)
            tracked.registrations.remove(registration)
            _maybe_release_entity_record(state, tracked)
        state.touch("rpc_connections", str(registration.source.connection_id))
        registration.source.entity_registrations.remove(registration)
        if registration.external_name is not None:
            if state.entity_external_names.get(registration.external_name) is registration:
                del state.entity_external_names[registration.external_name]


# --- Tracked record cleanup -----------------------------------------
#
# A tracked service / entity record stays alive as long as *either*
# its RPC ``registrations`` or its session ``tracking_sessions`` is
# non-empty. Whenever a registration releases or a session untracks,
# we run the corresponding ``_maybe_release_*_record`` to drop the
# record if both are now empty. Sync; called under the same atomicity
# guarantees as the release flow.


def maybe_release_service_record(
    state: WorkstationState,
    tracked: TrackedServiceRecord,
) -> None:
    """Drop ``tracked`` from the registry if it has no remaining
    holders. Triggers the worker's ``shutdown_event`` so
    ``service_scope`` drains."""
    if tracked.registrations or tracked.tracking_sessions:
        return
    state.service_records.pop(tracked.service_key, None)
    state.short_ids.deactivate_service(tracked.service_key)
    tracked.shutdown_event.set()


def _maybe_release_entity_record(
    state: WorkstationState,
    tracked: TrackedEntityRecord,
) -> None:
    """Drop ``tracked`` from the registry if it has no remaining
    holders. Drains the record's per-record :class:`FxtrScope`.

    Relies on the system invariant that every
    :class:`TrackedEntityRecord` owns its own scope holding the
    layered FxtrID — records are minted via
    :func:`_install_entity_record` only after the entity is in their
    scope, so the drain always has something to release. Other
    records that share part of the data subgraph keep their own
    arena claims and are unaffected.
    """
    if tracked.registrations or tracked.tracking_sessions:
        return
    key = provenanced_key_for(tracked.fxtr_id)
    state.entity_records.pop(key, None)
    state.short_ids.deactivate_entity(tracked.fxtr_id)
    tracked.scope.force_close()


# --- Service-break notification (out-of-band side channel) ----------
#
# When the grant backing a tracked service's bridge is revoked (peer
# revoke or RPC connection drop), the workstation's keep-alive lease on
# the service breaks. We observe that via a broken-callback on the
# bridge's standing lease (registered in ``service_record_worker``),
# mark the record ``broken``, surface the change on the ``services``
# resource, and push a sequence-less :class:`ServiceBrokenNotification`
# to every session subscription that currently holds the service. This
# is *not* a session event: it has no seq, never lands on the event
# log, and is re-derived from current state on every (re)subscribe
# (deduped per subscription via ``ClientConnectionRecord.announced_broken``).


def _announce_broken_service_to_subscription(
    conn: ClientConnectionRecord,
    sub_id: int,
    short_id: str,
) -> None:
    """Push one :class:`ServiceBrokenNotification` to a single session
    subscription, deduped via ``conn.announced_broken[sub_id]``. Sync;
    idempotent per (subscription, service)."""
    announced = conn.announced_broken.setdefault(sub_id, set())
    if short_id in announced:
        return
    announced.add(short_id)
    conn.send_nowait(ServiceBrokenNotification(subscription_id=sub_id, service_short_id=short_id))


def _announce_broken_service_to_session(
    state: WorkstationState,
    session_id: UUID,
    short_id: str,
) -> None:
    """Announce a broken service to every active subscription of one
    session. Sync."""
    for (_, sub_id), conn in list(state._session_subs.get(session_id, {}).items()):
        _announce_broken_service_to_subscription(conn, sub_id, short_id)


def _announce_break_for_record(
    state: WorkstationState,
    tracked: TrackedServiceRecord,
) -> None:
    """Announce ``tracked``'s break to every session currently holding
    it. ``tracking_sessions`` membership ⟺ ``service_refcounts > 0``
    (invariant III), so this is exactly the explicit-pin + implicit-ref
    notify set. Sync."""
    for session_id in list(tracked.tracking_sessions):
        _announce_broken_service_to_session(state, session_id, tracked.short_id)


def _on_service_lease_broken(
    state: WorkstationState,
    tracked: TrackedServiceRecord,
    reason: str,
) -> None:
    """Broken-callback for a tracked service's keep-alive lease.

    Fires synchronously when the gating grant is revoked / its RPC
    connection drops (possibly from the RPC reader task). Marks the
    record broken (one-way), surfaces the alive→broken flip on the
    ``services`` resource, and announces to every holding session.
    Idempotent — re-breaks are no-ops. Does only sync registry work +
    ``send_nowait``, consistent with the file's GIL-atomicity
    discipline; opens its own ``mutation_context`` for the resource
    delta.
    """
    if tracked.broken:
        return
    _log.info("tracked service %s broke: %s", tracked.short_id, reason)
    with state.mutation_context():
        # Touch before mutating so the change buffer's pre-image
        # captures the still-alive record.
        state.touch("services", tracked.short_id)
        tracked.broken = True
    _announce_break_for_record(state, tracked)


def _on_service_handle_token_ended(
    state: WorkstationState,
    service_key: ServiceKey,
) -> None:
    """Handle-token-end callback (registered in
    :func:`get_or_create_service_record`).

    The underlying handle has died, so the specific handle can never
    recur and its short-id mapping is reclaimable — *but only once the
    record is no longer alive*. While the record is still tracked /
    registered (e.g. a broken-but-still-tracked service) we keep the
    mapping resolvable so ``short_id_for_service`` /
    ``lookup_active_service`` and the debug snapshot don't break
    mid-flight. Once the record is released and the handle is dead, the
    mapping is dropped. Idempotent.
    """
    if service_key in state.service_records:
        return
    state.short_ids.forget_service(service_key)


# --- Session tracker bookkeeping ------------------------------------
#
# Sessions pin services / entities via the ``tracking_sessions`` set
# on each tracked record (rather than via Registration objects). The
# membership of ``tracking_sessions`` is governed by per-session
# refcount dicts (``entity_refcounts`` / ``service_refcounts``) — see
# invariants (I)–(IV) in ``stage3_sessions.md``. The four sync
# primitives below maintain both sides of the membership invariant;
# higher-level callers compose them.
#
# Layering:
#   - ``_incref_session_X`` / ``_decref_session_X`` are the raw
#     refcount primitives. They only manipulate the session-side
#     refcount dict and the corresponding ``tracking_sessions`` set;
#     they do *not* create or release the underlying ``TrackedXRecord``.
#     Caller's responsibility to ensure the record exists before
#     incref'ing (e.g. via the 3c.3a ``_get_or_create_X_record``
#     primitive that mints a fresh record + spawns the service worker).
#   - ``_add/remove_session_X_tracker`` are the explicit-pin
#     compositions used by ``TrackXOp`` / ``UntrackXOp``: they
#     update the pin set and call the corresponding incref/decref.
#   - ``_close_session_drain_trackers`` is the session-close path:
#     walks the refcount dicts and zeros them out, detaching the
#     session from every tracked record's ``tracking_sessions`` and
#     running ``_maybe_release_X_record`` for each.
#
# All sync; rely on GIL atomicity between awaits.


def _incref_session_entity(
    state: WorkstationState,
    session: SessionRecord,
    key: ProvenancedKey,
) -> None:
    """Sync primitive: bump the session's refcount on ``key``.

    On a 0→1 transition adds the session to the corresponding
    :class:`TrackedEntityRecord`'s ``tracking_sessions`` and notifies
    "entities" subscribers. The caller must have ensured the record
    exists in :attr:`WorkstationState.entity_records` first.
    """
    n = session.entity_refcounts.get(key, 0)
    session.entity_refcounts[key] = n + 1
    if n == 0:
        # 0→1: a new tracking_sessions edge.
        tracked = state.entity_records[key]
        state.touch("entities", tracked.short_id)
        tracked.tracking_sessions.add(session.session_id)
    _emit_session_debug_snapshot(state, session)


def _decref_session_entity(
    state: WorkstationState,
    session: SessionRecord,
    key: ProvenancedKey,
) -> None:
    """Sync primitive: decrement the session's refcount on ``key``.

    On a 1→0 transition removes the session from the
    :class:`TrackedEntityRecord`'s ``tracking_sessions``, runs
    :func:`_maybe_release_entity_record`, and notifies "entities"
    subscribers.

    Defensive on underflow (no refcount entry for ``key``):
    short-circuits without mutating state. Should not happen with
    correctly-paired calls but tolerating it keeps the close path
    simple if it interacts with stale state.
    """
    n = session.entity_refcounts.get(key, 0)
    if n == 0:
        return
    if n > 1:
        session.entity_refcounts[key] = n - 1
        _emit_session_debug_snapshot(state, session)
        return
    # 1→0 transition.
    del session.entity_refcounts[key]
    tracked = state.entity_records.get(key)
    if tracked is not None:
        state.touch("entities", tracked.short_id)
        tracked.tracking_sessions.discard(session.session_id)
        _maybe_release_entity_record(state, tracked)
    _emit_session_debug_snapshot(state, session)


def _incref_session_service(
    state: WorkstationState,
    session: SessionRecord,
    service_key: ServiceKey,
) -> None:
    """Sync primitive: bump the session's refcount on ``service_key``.

    On a 0→1 transition adds the session to the corresponding
    :class:`TrackedServiceRecord`'s ``tracking_sessions`` and notifies
    "services" subscribers. The caller must have ensured the record
    exists in :attr:`WorkstationState.service_records` first.
    """
    n = session.service_refcounts.get(service_key, 0)
    session.service_refcounts[service_key] = n + 1
    if n == 0:
        tracked = state.service_records[service_key]
        state.touch("services", tracked.short_id)
        tracked.tracking_sessions.add(session.session_id)
        # Trigger 2: the session newly holds a service that is
        # *already* broken — announce on this incref so a track of (or
        # a delivered payload referencing) an already-dead service
        # still surfaces the break.
        if tracked.broken:
            _announce_broken_service_to_session(state, session.session_id, tracked.short_id)
    _emit_session_debug_snapshot(state, session)


def _decref_session_service(
    state: WorkstationState,
    session: SessionRecord,
    service_key: ServiceKey,
) -> None:
    """Sync primitive: decrement the session's refcount on ``service_key``.

    On a 1→0 transition removes the session from the
    :class:`TrackedServiceRecord`'s ``tracking_sessions``, runs
    :func:`maybe_release_service_record`, and notifies "services"
    subscribers.
    """
    n = session.service_refcounts.get(service_key, 0)
    if n == 0:
        return
    if n > 1:
        session.service_refcounts[service_key] = n - 1
        _emit_session_debug_snapshot(state, session)
        return
    del session.service_refcounts[service_key]
    tracked = state.service_records.get(service_key)
    if tracked is not None:
        state.touch("services", tracked.short_id)
        tracked.tracking_sessions.discard(session.session_id)
        maybe_release_service_record(state, tracked)
    _emit_session_debug_snapshot(state, session)


def _add_session_entity_tracker(
    state: WorkstationState,
    session: SessionRecord,
    key: ProvenancedKey,
) -> None:
    """Composite for the explicit-pin path (:class:`TrackEntityOp`):
    add to the session's pin set, then bump refcount via
    :func:`_incref_session_entity`."""
    session.tracked_entities.add(key)
    _incref_session_entity(state, session, key)


def _remove_session_entity_tracker(
    state: WorkstationState,
    session: SessionRecord,
    key: ProvenancedKey,
) -> None:
    """Composite for the explicit-unpin path (:class:`UntrackEntityOp`):
    discard from the session's pin set, then decrement refcount via
    :func:`_decref_session_entity`."""
    session.tracked_entities.discard(key)
    _decref_session_entity(state, session, key)


def _add_session_service_tracker(
    state: WorkstationState,
    session: SessionRecord,
    service_key: ServiceKey,
) -> None:
    """Composite for the explicit-pin path (:class:`TrackServiceOp`)."""
    session.tracked_services.add(service_key)
    _incref_session_service(state, session, service_key)


def _remove_session_service_tracker(
    state: WorkstationState,
    session: SessionRecord,
    service_key: ServiceKey,
) -> None:
    """Composite for the explicit-unpin path (:class:`UntrackServiceOp`)."""
    session.tracked_services.discard(service_key)
    _decref_session_service(state, session, service_key)


def _close_session_drain_trackers(
    state: WorkstationState,
    session: SessionRecord,
) -> None:
    """Drain all session-side refcount contributions in one pass.

    Used by the session-close path. Walks ``entity_refcounts`` /
    ``service_refcounts`` (which by invariant (IV) include every
    contribution the session has made — explicit pins now, plus
    retained event refs and in-flight delivery contributions in
    3c.2b+); for each entry detaches the session from the
    corresponding ``TrackedXRecord.tracking_sessions``, runs
    ``_maybe_release_X_record``, and clears all session-side
    bookkeeping.

    Walks ``open_callbacks`` first, marking each record's impl as
    closed (for ready callbacks) or transitioning pending callbacks
    to ``cancelled``. The actual refcount cleanup for ready
    callbacks happens implicitly via the ``service_refcounts``
    drain below — open-callback contributions live in the same
    dict.

    All resource-side notifications are routed through the active
    :meth:`WorkstationState.mutation_context` buffer (the close path
    is invoked from inside :meth:`apply_mutation`, which owns the
    context). Each detach touches its tracked record before
    mutating; the buffer collapses repeated touches per key and
    emits one ``resource_changes`` frame per affected resource per
    subscriber.
    """
    # Open-callback sweep. Run first so impls are marked closed
    # before the service_refcounts drain releases them. Every
    # CallbackRecord is fully formed at this point — the pre-commit
    # phase that built it succeeded — so we just call
    # ``mark_closed()`` (flips ``_closed`` + wakes the impl's
    # subscription parker tasks for async unsubscribe) and let the
    # service_refcounts drain below pick up the open-callback
    # refcount contribution.
    while session.open_callbacks:
        slot = next(iter(session.open_callbacks))
        cb_record = session.open_callbacks.pop(slot)
        cb_record.impl.mark_closed()

    # 5e.1: clear open-invoke / open-load surface. The workers
    # themselves will eventually drain (their dispatches may still
    # complete and route through the no-op aborted_orphan branches
    # in the delivery helpers), but their open-slot debug entries
    # are gone the moment the session is closing.
    session.open_invokes.clear()
    session.open_loads.clear()

    drained_anything = bool(session.entity_refcounts) or bool(session.service_refcounts)
    while session.entity_refcounts:
        # `next(iter(...))` peeks at one key without consuming the
        # iterator (which would be invalidated by the upcoming `del`).
        entity_key = next(iter(session.entity_refcounts))
        del session.entity_refcounts[entity_key]
        tracked = state.entity_records.get(entity_key)
        if tracked is not None:
            state.touch("entities", tracked.short_id)
            tracked.tracking_sessions.discard(session.session_id)
            _maybe_release_entity_record(state, tracked)
    session.tracked_entities.clear()
    while session.service_refcounts:
        service_key = next(iter(session.service_refcounts))
        del session.service_refcounts[service_key]
        tracked = state.service_records.get(service_key)
        if tracked is not None:
            state.touch("services", tracked.short_id)
            tracked.tracking_sessions.discard(session.session_id)
            maybe_release_service_record(state, tracked)
    session.tracked_services.clear()
    session.event_refs.clear()
    # 8a: drop the preserved-events sidecar. Refcount contributions
    # for these events were already accounted for in the
    # ``entity_refcounts`` / ``service_refcounts`` drains above (the
    # sidecar holds the events themselves; refs live in
    # ``event_refs`` which we just cleared).
    session.preserved_events.clear()
    # One final empty-refcount snapshot to any debug subscribers
    # before close-time teardown removes their subscription.
    if drained_anything:
        _emit_session_debug_snapshot(state, session)


# --- Result delivery helpers (3c.2b+) -------------------------------
#
# Slot-result delivery follows the three-phase state machine described
# in ``stage3_sessions.md`` ("Result delivery state machine"):
#
#   phase 1 (sync): walk the value, attach each ref to the session via
#     ``_get_or_create_X_record`` + ``_incref_session_X``, under a
#     session-alive gate. Bail (silently) if session is gone.
#
#   phase 2 (async): for invoke results, await ``bridge_ready`` on
#     each newly-spawned service worker. Empty for load (CBOR has no
#     ServiceRefs). Lands in 3c.3b.
#
#   phase 3 (sync): under the session-alive gate, encode the value
#     via ``escape``, build a result event, and append + broadcast
#     via ``_append_session_event``. If the session went away during
#     phase 2, this is the ``aborted_orphan`` path — session-close
#     already drained our in-flight refcount contributions.
#
# Phase 1 and phase 3 are both sync critical sections. The codec
# (``escape``) is pure; ``_append_session_event`` does the seq
# allocation, ``event_refs`` bookkeeping, and SessionEventFrame
# broadcast.


def _open_record_scope(state: WorkstationState, fxtr_id: FxtrID) -> FxtrScope:
    """Open a fresh per-record :class:`FxtrScope` ready to hold
    ``fxtr_id``'s layered form.

    Isolated, non-mountable, child of :attr:`WorkstationState.root_scope`.
    Constructed sync; drained sync via ``force_close`` on release.
    """
    return FxtrScope(
        isolated=True,
        include=[state.root_scope],
        name=f"entity/{fxtr_id.id_string(include_provenance=True)}",
    ).lowlevel_start_lifespanless(
        parent_leases={state.root_scope: state.root_scope.clone_authorization()}
    )


def _install_entity_record(
    state: WorkstationState,
    fxtr_id: FxtrID,
    record_scope: FxtrScope,
) -> TrackedEntityRecord:
    """Sync: install a freshly-built :class:`TrackedEntityRecord` for
    ``fxtr_id`` carrying ``record_scope`` into the registry.

    Caller has already opened ``record_scope`` and brought ``fxtr_id``
    into it (via ``copy_from_scope`` or ``track_from_scope``). Caller
    is expected to be inside a :meth:`WorkstationState.mutation_context`.

    Touches the ``entities`` resource and allocates a short ID
    (idempotent — same FxtrID re-mints the same short ID across the
    server's lifetime).
    """
    key = provenanced_key_for(fxtr_id)
    short_id = state.short_ids.get_or_assign_entity(fxtr_id)
    state.touch("entities", short_id)
    tracked = TrackedEntityRecord(fxtr_id=fxtr_id, short_id=short_id, scope=record_scope)
    state.entity_records[key] = tracked
    return tracked


async def _ensure_entity_record_via_copy(
    state: WorkstationState,
    fxtr_id: FxtrID,
    source_scope: FxtrScope,
) -> TrackedEntityRecord:
    """Get-or-create the :class:`TrackedEntityRecord` for ``fxtr_id``,
    copying its data from ``source_scope`` if a new record needs to
    be minted.

    Async because :meth:`FxtrScope.copy_from_scope` may pull data from
    the source. For same-process same-store sources it's typically
    fast (just a DAG walk + lifetime claims).

    Strategy:

    1. Sync fast-path: if a record already exists for this key, return
       it. No copy.
    2. Otherwise open a fresh per-record scope and ``copy_from_scope``
       the layered FxtrID into it. (Async.)
    3. Sync atomic block (own ``mutation_context``): re-check
       (concurrent mint of same key is possible across awaits).
       On race-loss: drain our just-opened scope and return the
       winning record. Otherwise install our record + scope in the
       registry.

    The internal ``mutation_context`` covers only the install touch.
    Callers typically open their own ``mutation_context`` around any
    follow-up bookkeeping (e.g. session refcount increfs); having a
    separate context for the install means we'll emit two delta
    frames in that case (one for the mint, one for the incref). That
    extra frame is acceptable in exchange for the simpler API.
    """
    key = provenanced_key_for(fxtr_id)
    existing = state.entity_records.get(key)
    if existing is not None:
        return existing

    record_scope = _open_record_scope(state, fxtr_id)
    try:
        await record_scope.copy_from_scope(fxtr_id, source_scope)
    except BaseException:
        record_scope.force_close()
        raise

    # Sync atomic block: race re-check + install.
    with state.mutation_context():
        existing = state.entity_records.get(key)
        if existing is not None:
            record_scope.force_close()
            return existing
        return _install_entity_record(state, fxtr_id, record_scope)


def _ensure_entity_record_via_track(
    state: WorkstationState,
    fxtr_id: FxtrID,
    source_scope: FxtrScope,
) -> TrackedEntityRecord:
    """Sync variant of :func:`_ensure_entity_record_via_copy` for
    same-store in-memory transfers.

    Used for load-result children where the entity's data has already
    been brought into a transient ``load_scope`` by ``load_cbor`` and
    is in memory; ``track_from_scope`` extends a lifetime claim
    without an async copy step.

    Caller is expected to be inside a
    :meth:`WorkstationState.mutation_context`.
    """
    key = provenanced_key_for(fxtr_id)
    existing = state.entity_records.get(key)
    if existing is not None:
        return existing
    record_scope = _open_record_scope(state, fxtr_id)
    try:
        record_scope.track_from_scope(fxtr_id, source_scope)
    except BaseException:
        record_scope.force_close()
        raise
    return _install_entity_record(state, fxtr_id, record_scope)


def _walk_fxtr_ids(value: Any) -> list[FxtrID]:
    """Walk ``value`` recursively and return every FxtrID it contains.

    Recognized leaf types:

    - bare :class:`FxtrID`
    - :class:`EntityRef` (yields ``.entity_id``)
    - :class:`RawWeakFxtrID` / :class:`RawStrongFxtrID` /
      :class:`RawFxtrEntityMarkerID` (the wrapper types that appear
      in CBOR loaded via ``load_cbor``; yields ``.inner_id``)

    Walks dicts (str-keyed) and lists/tuples recursively. Other
    primitives (None, bool, int, float, str, bytes) are leaves and
    yield nothing. Unknown types are silently ignored — this walker
    is used pre-encode, so any structural concerns surface from
    :func:`escape` later.

    Returns a list (preserves traversal order; duplicates retained
    so the caller can dedupe with a set).
    """
    out: list[FxtrID] = []
    _walk_refs_into(value, out, None)
    return out


def _walk_refs(value: Any) -> tuple[list[FxtrID], list[ServiceRef[Any]]]:
    """Walk ``value`` for both FxtrID-bearing leaves and ServiceRefs.

    Used by the invoke result delivery path which needs to find both
    kinds of refs in one pass. See :func:`_walk_fxtr_ids` for the
    FxtrID side; ServiceRefs are detected via ``isinstance``.
    Duplicates are retained — callers dedupe.
    """
    fxtr_ids: list[FxtrID] = []
    service_refs: list[ServiceRef[Any]] = []
    _walk_refs_into(value, fxtr_ids, service_refs)
    return fxtr_ids, service_refs


def _walk_refs_into(
    value: Any,
    fxtr_ids: list[FxtrID],
    service_refs: list[ServiceRef[Any]] | None,
) -> None:
    if isinstance(value, FxtrID):
        fxtr_ids.append(value)
        return
    if isinstance(value, EntityRef):
        fxtr_ids.append(value.entity_id)
        return
    if isinstance(value, (RawWeakFxtrID, RawStrongFxtrID, RawFxtrEntityMarkerID)):
        fxtr_ids.append(value.inner_id)
        return
    if isinstance(value, ServiceRef):
        if service_refs is not None:
            service_refs.append(value)
        return
    if isinstance(value, dict):
        for v in value.values():
            _walk_refs_into(v, fxtr_ids, service_refs)
        return
    if isinstance(value, (list, tuple)):
        for v in value:
            _walk_refs_into(v, fxtr_ids, service_refs)
        return
    # primitives & unknowns: nothing to collect


def _append_session_event(
    state: WorkstationState,
    session: SessionRecord,
    event: NonStateSessionEvent,
    *,
    in_flight_e: set[ProvenancedKey],
    in_flight_s: set[ServiceKey],
) -> None:
    """Sync primitive: append ``event`` to ``event_log``, record
    ``event_refs[event.seq] = (in_flight_e, in_flight_s)`` (only if
    non-empty — keeps the dict tidy), and broadcast a
    :class:`SessionEventFrame` to every subscriber of this session.

    Only :class:`NonStateSessionEvent`s are appended here —
    :class:`StateUpdateEvent`s are authoritative through the
    record's ``last_state_*`` fields and broadcast directly from
    :meth:`WorkstationState.apply_mutation` rather than going through
    this primitive (post-3-post-2 invariant).

    The caller is responsible for allocating ``event.seq`` (typically
    by bumping ``session.event_seq``) before constructing ``event`` —
    msgspec events are frozen, so we can't fill the seq in here.

    Result events are not mutator-driven, so there's no
    self-suppression here — every session subscriber receives the
    frame.
    """
    session.event_log.append(event)
    if in_flight_e or in_flight_s:
        session.event_refs[event.seq] = (in_flight_e, in_flight_s)
    for (_, sub_id), conn in list(state._session_subs.get(session.session_id, {}).items()):
        conn.send_nowait(SessionEventFrame(subscription_id=sub_id, event=event))


def _emit_callback_subscription_event(
    state: WorkstationState,
    session_id: UUID,
    slot: int,
    connected: bool,
    reason: str | None = None,
) -> None:
    """Append a :class:`CallbackSubscriptionEvent` for a callback inbox's
    subscription connection-state transition.

    Allocates a fresh seq off ``session.event_seq`` and appends through
    :func:`_append_session_event` with empty in-flight ref sets (the
    event carries no entity / service payload). Silent no-op if the
    session is gone — a transition for a session that already closed has
    nowhere to land.

    Sync; safe to call from an arbitrary task (the lease-broken callback
    fires from e.g. the RPC reader, and ``subscribe_to`` runs on the
    invoke worker) — it does only sync registry work + ``send_nowait``,
    consistent with the file's GIL-atomicity discipline. Mirrors the
    ``CallbackEvent`` append path.
    """
    session = state.sessions.get(session_id)
    if session is None:
        return
    session.event_seq += 1
    _append_session_event(
        state,
        session,
        CallbackSubscriptionEvent(
            seq=session.event_seq, slot=slot, connected=connected, reason=reason
        ),
        in_flight_e=set(),
        in_flight_s=set(),
    )


def _build_session_debug_snapshot(
    state: WorkstationState,
    session: SessionRecord,
    subscription_id: int,
) -> SessionDebugSnapshotEvent:
    """Construct a :class:`SessionDebugSnapshotEvent` for ``session``.

    Refcount keys are translated from internal :class:`FxtrID` /
    :class:`ServiceKey` to workstation short_ids via the short-id
    allocator. The allocator's mapping is run-lifetime, so a key
    that's been used at least once stays resolvable here even after
    the underlying record is released — which is fine, because the
    refcount dict only contains keys with a positive count, which
    means the record is still alive (per invariant III in
    ``stage3_sessions.md``).
    """
    return SessionDebugSnapshotEvent(
        subscription_id=subscription_id,
        session_id=str(session.session_id),
        entity_refcounts={
            state.short_ids.short_id_for_entity(key.to_layered_fxtr_id()): count
            for key, count in session.entity_refcounts.items()
        },
        service_refcounts={
            state.short_ids.short_id_for_service(sk): count
            for sk, count in session.service_refcounts.items()
        },
        slot_counter=session.slot_counter,
        open_callbacks={slot: cb.service_short_id for slot, cb in session.open_callbacks.items()},
        open_invokes={
            slot: OpenInvokeInfo(
                service_short_id=state.short_ids.short_id_for_service(rec.service_key),
                method_name=rec.method_name,
            )
            for slot, rec in session.open_invokes.items()
        },
        open_loads={
            slot: state.short_ids.short_id_for_entity(key.to_layered_fxtr_id())
            for slot, key in session.open_loads.items()
        },
        preserved_events=sorted(session.preserved_events),
    )


def _emit_session_debug_snapshot(
    state: WorkstationState,
    session: SessionRecord,
) -> None:
    """Push a fresh :class:`SessionDebugSnapshotEvent` to every active
    debug subscriber of ``session``.

    Called as a hook from the refcount primitives
    (:func:`_incref_session_entity` / :func:`_decref_session_entity`
    and the service variants), the slot-counter bump in
    :meth:`WorkstationState.apply_mutation`, and
    :func:`_close_session_drain_trackers`. No-op if no subscribers.

    Sync; callable between awaits. Iterates the
    :attr:`_session_debug_subs` index and builds one snapshot per
    subscription.
    """
    for (_, sub_id), conn in list(state._session_debug_subs.get(session.session_id, {}).items()):
        conn.send_nowait(_build_session_debug_snapshot(state, session, sub_id))


def _phase1_attach_load_refs(
    state: WorkstationState,
    session_id: UUID,
    cbor: Any,
    load_scope: FxtrScope,
    *,
    in_flight_e: set[ProvenancedKey],
) -> bool:
    """Phase 1 of load-result delivery. Sync.

    Walks ``cbor`` for FxtrID-bearing leaves, get-or-creates a
    :class:`TrackedEntityRecord` for each via
    :func:`_ensure_entity_record_via_track` (sync, same-store
    transfer from the transient ``load_scope``), increments the
    session's refcount via :func:`_incref_session_entity`, and
    accumulates the attached :class:`ProvenancedKey`s into
    ``in_flight_e``.

    ``load_scope`` is the transient scope opened by :func:`load_worker`
    around the parent record's scope; ``load_cbor`` ran in
    ``load_scope``, so all child FxtrIDs are tracked there and their
    data is in memory. ``track_from_scope`` extends a lifetime claim
    into each child's per-record scope without copying bytes.

    Returns ``True`` if all refs attached. Returns ``False`` if the
    session is gone — in that case ``in_flight_e`` is empty (we don't
    attach if there's no session to attach to).
    """
    session = state.sessions.get(session_id)
    if session is None or session.closed:
        return False
    # Dedupe: a CBOR can mention the same child entity multiple times
    # (e.g. via $fxtr.type plus a payload field). One refcount bump
    # per distinct ``ProvenancedKey`` is enough — that's the
    # per-event-per-ref accounting in invariant (IV).
    seen: set[ProvenancedKey] = set()
    for fid in _walk_fxtr_ids(cbor):
        key = provenanced_key_for(fid)
        if key in seen:
            continue
        seen.add(key)
        _ensure_entity_record_via_track(state, fid, load_scope)
        _incref_session_entity(state, session, key)
        in_flight_e.add(key)
    return True


def _no_services_in_load(_ref: ServiceRef[Any]) -> str:
    raise AssertionError("CBOR cannot contain ServiceRef leaves")


def _phase3_commit_load_result(
    state: WorkstationState,
    session_id: UUID,
    slot: int,
    cbor: Any,
    in_flight_e: set[ProvenancedKey],
    *,
    silent: bool,
) -> None:
    """Phase 3 of load-result delivery. Sync.

    Under the session-alive gate, build a result event and append +
    broadcast via :func:`_append_session_event`. On session-gone (the
    ``aborted_orphan`` terminal state), no-op — session-close already
    drained our in-flight refcount contributions, so there's nothing
    left to undo.

    Two flavors of result event:
    - ``silent=False`` (default, :class:`LoadEntityOp`): escape ``cbor``
      via the wire codec and emit a :class:`LoadResultEvent` with the
      escaped payload. If :func:`escape` fails, transitions to the
      ``rolling_back`` terminal: decrements each in-flight ref via
      :func:`_decref_session_entity` and delivers a
      :class:`LoadErrorEvent` instead.
    - ``silent=True`` (8c, :class:`LoadEntitySilentOp`): skip the
      escape walk entirely (it's wasted work — the payload is
      discarded) and emit a :class:`LoadResultSilentEvent` carrying
      no payload. The phase-1 :func:`_phase1_attach_load_refs` walk
      already established the in-flight refcount contributions via
      its own FxtrID walk, so no part of the side-effect machinery
      depends on the escape walk.
    """
    session = state.sessions.get(session_id)
    if session is None or session.closed:
        return
    if silent:
        session.event_seq += 1
        silent_event: SessionEvent = LoadResultSilentEvent(seq=session.event_seq, slot=slot)
        _append_session_event(
            state,
            session,
            silent_event,
            in_flight_e=in_flight_e,
            in_flight_s=set(),
        )
        return
    try:
        payload = escape(
            cbor,
            encode_entity=state.short_ids.short_id_for_entity,
            encode_service=_no_services_in_load,
        )
    except WireEscapeError as exc:
        for key in in_flight_e:
            _decref_session_entity(state, session, key)
        session.event_seq += 1
        error_event = LoadErrorEvent(seq=session.event_seq, slot=slot, message=repr(exc))
        _append_session_event(state, session, error_event, in_flight_e=set(), in_flight_s=set())
        return
    session.event_seq += 1
    event = LoadResultEvent(seq=session.event_seq, slot=slot, payload=payload)
    _append_session_event(
        state,
        session,
        event,
        in_flight_e=in_flight_e,
        in_flight_s=set(),
    )


def _deliver_load_error(
    state: WorkstationState,
    session_id: UUID,
    slot: int,
    message: str,
) -> None:
    """Deliver a :class:`LoadErrorEvent`. Sync.

    Used when the load failed before phase 1 attached anything
    (``load_cbor`` raised, or the CBOR walk surfaced an unexpected
    structure). On session-gone, no-op.

    The error path has no in-flight refs to roll back (we get here
    before phase 1 succeeded). 3c.3b's invoke path adds a richer
    rollback for bridge errors mid-phase-2.
    """
    session = state.sessions.get(session_id)
    if session is None or session.closed:
        return
    session.event_seq += 1
    event = LoadErrorEvent(seq=session.event_seq, slot=slot, message=message)
    _append_session_event(
        state,
        session,
        event,
        in_flight_e=set(),
        in_flight_s=set(),
    )


def _drop_in_flight_arg_refs(
    state: WorkstationState,
    session_id: UUID,
    arg_in_flight_e: set[ProvenancedKey],
    arg_in_flight_s: set[ServiceKey],
) -> None:
    """Drop the in-flight arg-ref contributions held by the calling
    session for an invoke / load worker.

    Called from the worker's ``finally`` block so the contributions
    are released even on cancellation / dispatch error. If the
    session is gone (closed during the worker's lifetime), the
    close drain has already cleared the session's refcount dicts,
    so the per-key decrefs short-circuit harmlessly via the existing
    underflow guard in :func:`_decref_session_*`.
    """
    if not arg_in_flight_e and not arg_in_flight_s:
        return
    session = state.sessions.get(session_id)
    if session is None:
        return
    with state.mutation_context():
        for key in arg_in_flight_e:
            _decref_session_entity(state, session, key)
        for sk in arg_in_flight_s:
            _decref_session_service(state, session, sk)


def _drop_open_invoke_slot(state: WorkstationState, session_id: UUID, slot: int) -> None:
    """Pop ``slot`` from :attr:`SessionRecord.open_invokes` and emit
    a fresh debug snapshot. Called from :func:`invoke_worker`'s
    ``finally`` block so the entry is removed on success, error, and
    cancellation paths uniformly. No-op if the session is gone or
    the entry is already absent (e.g. ``_close_session_drain_trackers``
    cleared it during teardown)."""
    session = state.sessions.get(session_id)
    if session is None:
        return
    if session.open_invokes.pop(slot, None) is None:
        return
    _emit_session_debug_snapshot(state, session)


def _drop_open_load_slot(state: WorkstationState, session_id: UUID, slot: int) -> None:
    """Pop ``slot`` from :attr:`SessionRecord.open_loads` and emit a
    fresh debug snapshot. Counterpart to :func:`_drop_open_invoke_slot`
    for the load pipeline."""
    session = state.sessions.get(session_id)
    if session is None:
        return
    if session.open_loads.pop(slot, None) is None:
        return
    _emit_session_debug_snapshot(state, session)


async def deliver_load_result_to_slot(
    state: WorkstationState,
    session_id: UUID,
    slot: int,
    cbor: Any,
    load_scope: FxtrScope,
    *,
    silent: bool,
) -> None:
    """Three-phase delivery for a successful CBOR load.

    Async (phase 2 is empty for load — CBOR carries no ServiceRefs —
    but kept async for parity with the upcoming invoke variant in
    3c.3b). Phase 3 owns its own error handling (codec failures
    decref the in-flight refs and deliver a ``LoadErrorEvent``), so
    this helper is just sequence-and-glue.

    ``load_scope`` is the transient scope opened by :func:`load_worker`
    where ``load_cbor`` ran; phase 1 uses it as the source for
    ``track_from_scope`` when minting per-record scopes for child
    entities discovered in the CBOR.

    ``silent`` (8c) selects between the
    :class:`~fxtr_workstation.client_protocol.LoadResultEvent` and
    :class:`~fxtr_workstation.client_protocol.LoadResultSilentEvent`
    success paths. The phase-1 attach walks FxtrIDs and increfs the
    session regardless, so child entities are tracked uniformly; the
    flag only affects phase-3 event construction.

    Phase 1 and phase 3 each open their own
    :meth:`WorkstationState.mutation_context` because they're
    separate sync windows: phase 1 attaches refs (which touches the
    ``entities`` resource via ``_incref_session_entity``); the
    no-op phase 2 follows; phase 3 either commits the event or
    rolls back via per-ref decref. Each window's resource changes
    flush at its own boundary.
    """
    in_flight_e: set[ProvenancedKey] = set()
    with state.mutation_context():
        if not _phase1_attach_load_refs(
            state, session_id, cbor, load_scope, in_flight_e=in_flight_e
        ):
            return  # aborted_silent
    # Phase 2: empty for load.
    with state.mutation_context():
        _phase3_commit_load_result(state, session_id, slot, cbor, in_flight_e, silent=silent)


async def load_worker(
    state: WorkstationState,
    session_id: UUID,
    slot: int,
    fxtr_id: FxtrID,
    arg_in_flight_e: set[ProvenancedKey],
    arg_in_flight_s: set[ServiceKey],
    silent: bool,
) -> None:
    """Background worker spawned per :class:`LoadEntityOp` /
    :class:`LoadEntitySilentOp`.

    Looks up the parent record (op-validation guarantees it exists),
    opens a transient ``load_scope`` as a child of the parent
    record's scope, and runs ``load_cbor`` in it. The transient
    scope keeps any cached intermediates from polluting the parent's
    own per-record scope; it drains automatically when the worker
    exits. On a regular ``Exception`` (entity not loadable,
    deserialization error, etc.) delivers a :class:`LoadErrorEvent`
    via :func:`_deliver_load_error` — no in-flight refs to roll back
    at that point. ``BaseException`` (cancellation, KeyboardInterrupt,
    etc.) is *not* caught — those propagate up to the task group so
    structured-concurrency teardown works correctly. On success,
    hands off to :func:`deliver_load_result_to_slot`, which uses the
    transient ``load_scope`` as the source for child-record minting
    via ``track_from_scope``.

    ``silent`` (8c) selects between :class:`LoadResultEvent` (false)
    and :class:`LoadResultSilentEvent` (true) for the success path.
    Failures still deliver :class:`LoadErrorEvent` regardless —
    silent / non-silent only differ on success.

    ``arg_in_flight_e`` / ``arg_in_flight_s`` are the calling
    session's in-flight contributions for this load's args (the
    entity being loaded, today; both sets shapes mirror the invoke
    worker for symmetry). They were incref'd by ``apply_mutation``
    before the worker was spawned and are dropped here in a
    ``finally`` block — cancellation / dispatch errors don't leak
    refcounts.
    """
    try:
        parent_record = state.entity_records.get(provenanced_key_for(fxtr_id))
        if parent_record is None:
            with state.mutation_context():
                _deliver_load_error(
                    state,
                    session_id,
                    slot,
                    f"entity record for {fxtr_id.id_string()} disappeared "
                    "before load_worker started",
                )
            return
        load_scope = FxtrScope(
            isolated=True,
            include=[parent_record.scope],
            name=f"load/{slot}/{fxtr_id.id_string()}",
        ).lowlevel_start_lifespanless(
            parent_leases={parent_record.scope: parent_record.scope.clone_authorization()}
        )
        try:
            try:
                cbor = await load_scope.load_cbor(fxtr_id)
            except Exception as exc:
                with state.mutation_context():
                    _deliver_load_error(state, session_id, slot, repr(exc))
                return
            await deliver_load_result_to_slot(
                state, session_id, slot, cbor, load_scope, silent=silent
            )
        finally:
            load_scope.force_close()
    finally:
        _drop_open_load_slot(state, session_id, slot)
        _drop_in_flight_arg_refs(state, session_id, arg_in_flight_e, arg_in_flight_s)


# --- Out-of-band direct entity load (stage 8b) ----------------------


async def direct_load_worker(
    state: WorkstationState,
    record: ClientConnectionRecord,
    request_id: int,
    fxtr_id: FxtrID,
) -> None:
    """Background worker spawned per :class:`LoadEntityRequest`.

    Looks up the parent record (handler-side validation guarantees it
    existed at request time; race-checked here), opens a transient
    ``load_scope`` as a child of the parent record's scope, runs
    ``load_cbor``, escapes the result with the *inactive* short-id
    allocator (so children FxtrIDs in the payload appear as
    descriptors with stable run-lifetime short_ids but are *not*
    minted as :class:`TrackedEntityRecord`s), and replies on the
    requesting connection's send queue.

    No session linkage. No refcounts touched. The transient scope
    drains at the end of the worker.

    Failure modes are uniform: the parent disappeared, ``load_cbor``
    raised, or ``escape`` failed. Each becomes a single
    :class:`LoadEntityErrorResponse` carrying ``repr(exc)`` (or a
    short message for the disappeared-parent case). On a closed
    connection ``record.send_nowait`` returns ``False`` and the
    response is discarded — no exception escapes. ``BaseException``
    (cancellation, KeyboardInterrupt) is *not* caught.
    """
    parent_record = state.entity_records.get(provenanced_key_for(fxtr_id))
    if parent_record is None:
        record.send_nowait(
            LoadEntityErrorResponse(
                request_id=request_id,
                message=(
                    f"entity record for {fxtr_id.id_string()} disappeared "
                    "before direct_load_worker started"
                ),
            )
        )
        return
    load_scope = FxtrScope(
        isolated=True,
        include=[parent_record.scope],
        name=f"direct_load/{request_id}/{fxtr_id.id_string()}",
    ).lowlevel_start_lifespanless(
        parent_leases={parent_record.scope: parent_record.scope.clone_authorization()}
    )
    try:
        try:
            cbor = await load_scope.load_cbor(fxtr_id)
        except Exception as exc:
            record.send_nowait(LoadEntityErrorResponse(request_id=request_id, message=repr(exc)))
            return
        try:
            payload = escape(
                cbor,
                encode_entity=state.short_ids.get_or_assign_entity_inactive,
                encode_service=_no_services_in_load,
            )
        except WireEscapeError as exc:
            record.send_nowait(LoadEntityErrorResponse(request_id=request_id, message=repr(exc)))
            return
        record.send_nowait(LoadEntityResponse(request_id=request_id, payload=payload))
    finally:
        load_scope.force_close()


# --- Invoke result delivery (3c.3b) ---------------------------------


def _format_invoke_error(exc: BaseException) -> str:
    """Render an invoke failure for an :class:`InvokeErrorEvent.message`.

    The full ``traceback.format_exception`` string, not ``repr(exc)``.
    Invoke failures arrive here as wire-decoded ``IRException``s whose
    useful detail (the originating exception type + the formatted
    cross-process traceback) lives in ``str(exc)`` / ``traceback_info``,
    not in ``repr`` (which shows only the bare ``.message``). The agent
    has no other channel to recover it (unlike script errors, there is
    no ``get_full_exception`` escape hatch for invokes), so deliver the
    whole thing inline. ``message`` is unbounded text — nothing
    downstream truncates."""
    return "".join(traceback.format_exception(exc))


def _deliver_invoke_error(
    state: WorkstationState,
    session_id: UUID,
    slot: int,
    message: str,
) -> None:
    """Deliver an :class:`InvokeErrorEvent`. Sync.

    Used for early failures (dispatch raised before any refs were
    attached). On session-gone, no-op.
    """
    session = state.sessions.get(session_id)
    if session is None or session.closed:
        return
    session.event_seq += 1
    event = InvokeErrorEvent(seq=session.event_seq, slot=slot, message=message)
    _append_session_event(state, session, event, in_flight_e=set(), in_flight_s=set())


def _decref_in_flight_session_refs(
    state: WorkstationState,
    session_id: UUID,
    in_flight_e: set[ProvenancedKey],
    in_flight_s: set[ServiceKey],
) -> None:
    """Decref this session's in-flight contributions for a delivery
    pipeline that's rolling back.

    Sync; opens its own :meth:`WorkstationState.mutation_context` so
    the resource changes flush even when the helper is propagating
    an exception out to its caller. If the session is gone
    (aborted_orphan), close-drain has already cleared the refcount
    dicts, so this is a no-op.
    """
    if not in_flight_e and not in_flight_s:
        return
    session = state.sessions.get(session_id)
    if session is None or session.closed:
        return
    with state.mutation_context():
        for fid in in_flight_e:
            _decref_session_entity(state, session, fid)
        for sk in in_flight_s:
            _decref_session_service(state, session, sk)


async def _deliver_result_as_event(
    state: WorkstationState,
    session_id: UUID,
    result: Any,
    source_scope: FxtrScope,
    *,
    build_result_event: Callable[[int, Any], NonStateSessionEvent],
) -> bool:
    """Shared 3-phase delivery for a Python result that may carry
    :class:`FxtrID` and :class:`ServiceRef` leaves.

    Used by both invoke-result delivery and ``notify`` (callback)
    delivery — they have identical phase shapes; only the event class
    and the caller's failure handling differ.

    The system invariant is that every :class:`TrackedEntityRecord`
    owns its own per-record :class:`FxtrScope` containing the
    layered FxtrID — records are not minted before their scope
    holds the data, and not left behind without their scope (a
    drain on release closes both). Services are different: their
    record is minted before :attr:`TrackedServiceRecord.bridge_ready`
    because the worker that establishes the bridge needs the record
    to attach to.

    - **Phase 1 (sync atomic, under session-alive gate)**: mint
      service records via :func:`get_or_create_service_record`
      (which spawns a worker on first creation) and incref the
      session for each. Just collect entity FxtrIDs — no entity
      records yet.
    - **Phase 2 (async, parallel via task group)**:
      - per service: ``await bridge_ready``; raise on bridge_error.
      - per entity: :func:`_ensure_entity_record_via_copy` (which
        opens a fresh per-record scope and ``copy_from_scope``s the
        layered FxtrID into it on first mint), then — in the
        following sync block — either :func:`_incref_session_entity`
        (if session alive) or :func:`_maybe_release_entity_record`
        (if session is gone, the just-minted record has no holder
        so it's released and its scope drains).
      - any task raising propagates through the task group, which
        cancels siblings; the helper decref's in-flight refs and
        re-raises.
    - **Phase 3 (sync)**: encode the result via the list-based
      escape codec, build an event via ``build_result_event(seq,
      payload)``, and append + broadcast via
      :func:`_append_session_event`. Codec errors decref in-flight
      refs and re-raise.

    Args:
        state: The workstation state. The helper reads the session
            registry, mints / increfs entity + service records, and
            broadcasts the resulting event through it.
        session_id: The session whose event log the result lands on.
            The session is the *receive* side; for invoke-result
            delivery this is the session that issued the
            :class:`InvokeOp`, for ``notify`` delivery this is the
            session that owns the receive-side callback slot. The
            session is re-looked-up at each sync window (phase 1
            entry, post-phase-2 entry, phase 3 commit) so a
            concurrent close transitions cleanly to the bool-return
            paths instead of leaking refcounts.
        result: The Python value to walk + encode + deliver. Any
            :class:`FxtrID` / :class:`EntityRef` / :class:`ServiceRef`
            leaves found by :func:`_walk_refs` are tracked against
            the session as in-flight contributions; the rest of the
            value is passed through to the list-based escape codec
            and lands as the event's ``payload``.
        source_scope: The scope the helper looks in for entity data
            when an :class:`FxtrID` leaf isn't yet a tracked record
            (per-record scope opened on first mint via
            :func:`_ensure_entity_record_via_copy`). For invoke this
            is the dispatch ``call_scope`` opened by
            ``_invoke_worker_body``; for
            notify / subscription deliveries this is the dispatched
            method's active scope (``get_active_scope()`` inside
            :meth:`WorkstationInboxImpl.notify` /
            ``_InboxSubscriberImpl.on_event``). Service refs
            don't consult this directly —
            :func:`get_or_create_service_record` re-uses the source
            scope as the bridge's parent, and the per-record
            ``service_record_worker`` then owns the bridged scope
            independently.
        build_result_event: Factory invoked once during phase 3 with
            ``(seq, escaped_payload)`` to produce the
            :class:`NonStateSessionEvent` that's appended + broadcast.
            Lets invoke build an :class:`InvokeResultEvent` and notify
            build a :class:`CallbackEvent` from the same pipeline;
            also captures the slot from the caller's closure.

    Returns:
        ``True`` — the result event was committed to the session's
            event log.
        ``False`` — the session went away before phase 1 (no in-flight
            refs were minted) or before phase 3 commit (close-drain
            cleared the contributions). The caller should treat this
            as "stop sending".

    Raises:
        Any exception from phase-2 bridge-up or phase-3 codec failure,
        propagated from the underlying machinery. Before raising, this
        helper has decref'd any in-flight contributions it made, so
        the caller is responsible only for deciding what to surface
        (e.g. invoke catches and emits an :class:`InvokeErrorEvent`;
        notify lets the exception propagate to the RPC caller).
    """
    raw_fxtr_ids, raw_service_refs = _walk_refs(result)
    # Dedupe by ProvenancedKey rather than by FxtrID equality so that
    # ``(data, prov_X)`` and ``(data, prov_Y)`` are treated as distinct
    # entities — FxtrID's ``__eq__`` ignores ``_provenance_parent``.
    fxtr_ids_by_key: dict[ProvenancedKey, FxtrID] = {}
    for fid in raw_fxtr_ids:
        fxtr_ids_by_key.setdefault(provenanced_key_for(fid), fid)
    service_refs_by_key: dict[ServiceKey, ServiceRef[Any]] = {}
    for sref in raw_service_refs:
        sk = ServiceKey(handle=sref.handle, type_id=sref.service_type_id)
        service_refs_by_key.setdefault(sk, sref)

    # Phase 1 (sync atomic): session-alive check + service mint+incref.
    # Entities are deferred to phase 2 — their records are minted
    # only once their data is in scope.
    session = state.sessions.get(session_id)
    if session is None or session.closed:
        return False  # aborted_silent — nothing minted

    in_flight_e: set[ProvenancedKey] = set()
    in_flight_s: set[ServiceKey] = set()
    new_service_records: list[TrackedServiceRecord] = []
    # Phase 1 sync window: services minted + incref'd. ``services``
    # touches happen inside ``get_or_create_service_record`` (on
    # first creation) and ``_incref_session_service`` (on 0→1).
    with state.mutation_context():
        for sk, sref in service_refs_by_key.items():
            was_new = sk not in state.service_records
            record = get_or_create_service_record(state, sref, source_scope)
            if was_new:
                new_service_records.append(record)
            _incref_session_service(state, session, sk)
            in_flight_s.add(sk)

    # Phase 2 (async, parallel): bridge waits + per-entity copy +
    # mint + (incref or release). All children share ``in_flight_e``
    # — they only mutate it inside their post-await sync block, and
    # those sync blocks don't interleave (no awaits inside), so the
    # ``set.add`` calls are GIL-atomic. Each per-entity sync block
    # opens its own ``mutation_context``.
    try:
        async with create_task_group(collapse_exc_group=True) as tg:
            for record in new_service_records:
                tg.start_soon(_phase2_await_bridge, record)
            for fid in fxtr_ids_by_key.values():
                tg.start_soon(
                    _phase2_attach_entity,
                    state,
                    session_id,
                    source_scope,
                    fid,
                    in_flight_e,
                )
    except Exception:
        _decref_in_flight_session_refs(state, session_id, in_flight_e, in_flight_s)
        raise

    # Phase 3 (sync): encode + append. Re-check session-alive after
    # the phase-2 task-group boundary (a close mutation could have run
    # while we were yielded). On aborted_orphan: close-drain already
    # cleared our contributions, just return False.
    session = state.sessions.get(session_id)
    if session is None or session.closed:
        return False  # aborted_orphan

    def encode_service_short_id(sref: ServiceRef[Any]) -> str:
        return state.short_ids.short_id_for_service(
            ServiceKey(handle=sref.handle, type_id=sref.service_type_id)
        )

    try:
        payload = escape(
            result,
            encode_entity=state.short_ids.short_id_for_entity,
            encode_service=encode_service_short_id,
        )
    except WireEscapeError:
        # Encode is pure — nothing committed yet. Decref our
        # contributions in a fresh mutation_context (so the
        # ``services`` / ``entities`` deltas flush) and re-raise.
        _decref_in_flight_session_refs(state, session_id, in_flight_e, in_flight_s)
        raise

    with state.mutation_context():
        session.event_seq += 1
        event = build_result_event(session.event_seq, payload)
        _append_session_event(
            state, session, event, in_flight_e=in_flight_e, in_flight_s=in_flight_s
        )
    return True


async def deliver_invoke_result_to_slot(
    state: WorkstationState,
    session_id: UUID,
    slot: int,
    result: Any,
    source_scope: FxtrScope,
) -> None:
    """Invoke-result wrapper around :func:`_deliver_result_as_event`.

    The shared helper either commits a :class:`InvokeResultEvent`,
    silently aborts (session gone), or raises. Invoke's failure
    surface is a session-side :class:`InvokeErrorEvent`, so we catch
    any exception out of the helper and emit one. The bool return is
    discarded — there's no caller-visible difference between commit
    and aborted_silent for the invoke pipeline.
    """
    try:
        await _deliver_result_as_event(
            state,
            session_id,
            result,
            source_scope,
            build_result_event=lambda seq, payload: InvokeResultEvent(
                seq=seq, slot=slot, payload=payload
            ),
        )
    except Exception as exc:
        with state.mutation_context():
            _deliver_invoke_error(state, session_id, slot, _format_invoke_error(exc))


# --- Metadata-query resolution (6c) ---------------------------------
#
# ``GetEntityMetadataOp`` / ``GetServiceMetadataOp`` resolve sync
# inside :meth:`WorkstationState.apply_mutation`'s commit phase. The
# helpers below compute a metadata dict and a list of refs that need
# to be incref'd against the requesting session for the lifetime of
# the result event (entered into ``event_refs[seq]`` for retention).
#
# Entity metadata fields are described on
# :class:`fxtr_workstation.client_protocol.GetEntityMetadataOp`. All
# fields are computed sync via public scope methods on a transient
# child of ``record.scope``: :meth:`FxtrScope.embed_as_provenance_parent`
# for ``unwraps_to``, :meth:`FxtrScope.c_node_to_context_data` for
# ``context_data``, and :meth:`FxtrScope.step_back_p_ctx` for
# ``backstep``.


def _compute_entity_metadata(
    state: WorkstationState,
    record: TrackedEntityRecord,
) -> tuple[dict[str, Any], list[ProvenancedKey]]:
    """Compute a :class:`GetEntityMetadataOp` result payload + the
    list of :class:`ProvenancedKey`s the session needs to incref.

    Sync. Caller must be inside a
    :meth:`WorkstationState.mutation_context`. Side effects: may
    mint new :class:`TrackedEntityRecord`s for fields whose short
    refs aren't yet allocated (notably ``unprovenanced``,
    ``provenance_parent``, and ``unwraps_to``); each new mint goes
    through :func:`_ensure_entity_record_via_track` from a transient
    child of ``record.scope`` so the per-record-scope invariant
    holds.

    Returns ``(metadata, ref_keys)`` where ``ref_keys`` is the
    deduped list of new ``ProvenancedKey``s the caller should
    incref.
    """
    layered_fid = record.fxtr_id
    data_id = FxtrID(raw_data_id=layered_fid._raw_data_id)
    prov_parent = layered_fid._provenance_parent

    # ``id_string`` carries only the local (data-half) id-string —
    # the provenance half is already surfaced via ``provenance_parent``
    # / ``provenance_parent_id_string`` when present, so the full
    # layered form would be redundant.
    metadata: dict[str, Any] = {
        "id_string": layered_fid.local_id_string(),
    }
    ref_keys: list[ProvenancedKey] = []

    # ``unprovenanced``: short ref of ``ProvenancedKey(data, None)``.
    # Always emitted. record.scope tracks both halves of the layered
    # FxtrID, so the bare data ID is reachable for track_from_scope.
    # Entity-ref fields are emitted in list-escape-tagged form
    # ``["entity", short_id]`` so the frontend's ``unescapeWire``
    # pipeline produces ``EntityRefStub`` instances directly.
    unprov_record = _ensure_entity_record_via_track(state, data_id, record.scope)
    metadata["unprovenanced"] = ["entity", unprov_record.short_id]
    ref_keys.append(ProvenancedKey(data_id=data_id, prov_parent=None))

    # ``provenance_parent`` + ``provenance_parent_id_string``.
    if prov_parent is not None:
        prov_record = _ensure_entity_record_via_track(state, prov_parent, record.scope)
        metadata["provenance_parent"] = ["entity", prov_record.short_id]
        metadata["provenance_parent_id_string"] = prov_parent.id_string()
        ref_keys.append(ProvenancedKey(data_id=prov_parent, prov_parent=None))

    # ``unwraps_to`` / ``unwraps_to_id_string`` (for ``c``/``n``/``p``/
    # ``u``-tag data IDs), ``context_data`` (for ``c``), and
    # ``backstep`` (for ``p`` with a recorded backstep): all rely on
    # sync arena methods that take a lease and a source FxtrID.
    # Open a single transient child scope of ``record.scope`` and
    # call them on its arena directly via
    # :attr:`FxtrScope._main_arena`.
    data_tag = data_id.get_tag()
    if data_tag in ("c", "n", "p", "u"):
        transient = FxtrScope(
            isolated=True,
            include=[record.scope],
            name=f"metadata/{record.short_id}",
        ).lowlevel_start_lifespanless(
            parent_leases={record.scope: record.scope.clone_authorization()}
        )
        try:
            transient.lift(data_id)

            # ``unwraps_to`` via ``embed_as_provenance_parent``.
            unwrap_layered = transient.embed_as_provenance_parent(data_id)
            unwrap_record = _ensure_entity_record_via_track(state, unwrap_layered, transient)
            metadata["unwraps_to"] = ["entity", unwrap_record.short_id]
            # Local-only — the prov half is already known to the
            # caller (it's the current record's data ID).
            metadata["unwraps_to_id_string"] = unwrap_layered.local_id_string()
            ref_keys.append(provenanced_key_for(unwrap_layered))

            # ``context_data`` for c-tag data: the d-node the c-node
            # was built from. Sync via
            # :meth:`FxtrScope.c_node_to_context_data`.
            if data_tag == "c":
                ctx_data_id = transient.c_node_to_context_data(data_id)
                ctx_record = _ensure_entity_record_via_track(state, ctx_data_id, transient)
                metadata["context_data"] = ["entity", ctx_record.short_id]
                ref_keys.append(provenanced_key_for(ctx_data_id))

            # ``backstep`` for p-tag data: the p-node parent in the
            # path (one step shorter). Sync via
            # :meth:`FxtrScope.step_back_p_ctx`. ``None`` when the
            # p-node has no recorded backstep (pathroot p-node).
            if data_tag == "p":
                backstep_id = transient.step_back_p_ctx(data_id)
                if backstep_id is not None:
                    bs_record = _ensure_entity_record_via_track(state, backstep_id, transient)
                    metadata["backstep"] = ["entity", bs_record.short_id]
                    ref_keys.append(provenanced_key_for(backstep_id))
        finally:
            transient.force_close()

    return metadata, ref_keys


def _compute_service_metadata(
    record: TrackedServiceRecord,
) -> tuple[dict[str, Any], list[ServiceKey]]:
    """Compute a :class:`GetServiceMetadataOp` result payload + the
    list of :class:`ServiceKey`s the session needs to incref.

    Currently exposes only ``type_id_string``. The list of
    :class:`ServiceKey`s is empty — the metadata fields don't carry
    short refs to other tracked services today; the field would be
    meaningful only once we surface e.g. a service's type entity as
    a tracked-entity short ref (a future extension).
    """
    return (
        {"type_id_string": record.service_key.type_id.id_string()},
        [],
    )


async def _phase2_await_bridge(record: TrackedServiceRecord) -> None:
    """Phase 2 task: wait for a freshly-minted service record's bridge
    to come up. Raises if the bridge errored — task-group cancellation
    then unwinds sibling tasks."""
    await record.bridge_ready.wait()
    if record.bridge_error is not None:
        raise record.bridge_error


async def _phase2_attach_entity(
    state: WorkstationState,
    session_id: UUID,
    source_scope: FxtrScope,
    fid: FxtrID,
    in_flight_e: set[ProvenancedKey],
) -> None:
    """Phase 2 task: bring entity ``fid`` into a per-record scope
    (or pick up an existing one), then atomically incref the session
    or release the freshly-minted record.

    Per-record scope model: every :class:`TrackedEntityRecord`
    carries its own isolated :class:`FxtrScope` containing the
    layered FxtrID. :func:`_ensure_entity_record_via_copy` opens a
    fresh scope and copies from ``source_scope`` if a new record
    needs to be minted, or returns an existing record otherwise.

    The 1-1 invariant — every record has its scope, every scope has
    its record — is preserved by the helper's race-checked install
    path and by :func:`_maybe_release_entity_record`'s drain.

    Cancellation (``BaseException``) propagates — anyio's task-group
    cancellation needs to flow through cleanly.
    """
    tracked = await _ensure_entity_record_via_copy(state, fid, source_scope)
    # Sync atomic block — no awaits past this point. Wrapped in a
    # ``mutation_context`` because the touches that
    # ``_incref_session_entity`` / ``_maybe_release_entity_record``
    # add need a buffer to flush against.
    with state.mutation_context():
        session = state.sessions.get(session_id)
        if session is None or session.closed:
            # Session is gone — release the record we just minted /
            # picked up if it has no other holders. The
            # ``_maybe_release_entity_record`` helper short-circuits
            # if other holders are still present.
            state.touch("entities", tracked.short_id)
            _maybe_release_entity_record(state, tracked)
            return
        key = provenanced_key_for(fid)
        _incref_session_entity(state, session, key)
        in_flight_e.add(key)


async def _await_service_bridge_or_raise(record: TrackedServiceRecord) -> FxtrScope:
    """Wait for ``record.bridge_ready``; raise if the bridge errored
    or if ``service_scope`` is somehow missing. Returns the bridged
    ``service_scope`` on success.

    Used by :func:`invoke_worker` to gate access to the dispatched-to
    service and its arg services through their workstation
    ``service_scope`` bridges.
    """
    await record.bridge_ready.wait()
    if record.bridge_error is not None:
        raise record.bridge_error
    if record.service_scope is None:
        raise RuntimeError(
            f"service record {record.short_id} bridge_ready was set but " "service_scope is unset"
        )
    return record.service_scope


async def invoke_worker(
    state: WorkstationState,
    session_id: UUID,
    slot: int,
    service_key: ServiceKey,
    method_name: str,
    resolved_args: list[Any],
    resolved_kwargs: dict[str, Any],
    arg_in_flight_e: set[ProvenancedKey],
    arg_in_flight_s: set[ServiceKey],
) -> None:
    """Background worker spawned per :class:`InvokeOp`.

    Opens a per-call ``call_scope`` that includes the dispatched-to
    service's ``service_scope``, every arg-service's ``service_scope``,
    and every arg-entity's per-record ``scope`` — so FxtrID args /
    results and the target ref are reachable via inheritance.
    Resolves the target ref to an :class:`UnknownServiceProxy` and
    dispatches ``method_name`` with the resolved args + kwargs.

    Dispatch failures (any regular ``Exception``) deliver an
    :class:`InvokeErrorEvent`. ``BaseException`` propagates so
    cancellation / shutdown teardown works correctly.

    On success, hands the result + ``call_scope`` off to
    :func:`deliver_invoke_result_to_slot`, which manages the multi-
    phase attach / await / commit pipeline. ``call_scope`` only
    drains after ``deliver_invoke_result_to_slot`` returns —
    bridges that newly-created service records establish from
    ``call_scope`` are "Independent" and survive that drain.

    ``arg_in_flight_e`` / ``arg_in_flight_s`` are the calling
    session's in-flight contributions for this invoke's args (the
    target service plus every entity / service ref reachable in
    ``resolved_args`` and ``resolved_kwargs``). They were incref'd
    by ``apply_mutation`` before the worker was spawned, and are
    dropped here in a ``finally`` block so the contributions don't
    leak under cancellation or dispatch errors. Concurrent untracks
    (in later mutations on this session, or on other sessions)
    decrement other contributions but our in-flight ones keep the
    records alive through the dispatch.
    """
    try:
        await _invoke_worker_body(
            state,
            session_id,
            slot,
            service_key,
            method_name,
            resolved_args,
            resolved_kwargs,
        )
    finally:
        _drop_open_invoke_slot(state, session_id, slot)
        _drop_in_flight_arg_refs(state, session_id, arg_in_flight_e, arg_in_flight_s)


async def _invoke_worker_body(
    state: WorkstationState,
    session_id: UUID,
    slot: int,
    service_key: ServiceKey,
    method_name: str,
    resolved_args: list[Any],
    resolved_kwargs: dict[str, Any],
) -> None:
    """Inner body of :func:`invoke_worker`. Split out so the outer
    function can wrap the whole pipeline in a single ``finally`` for
    arg-ref decref."""
    target_record = state.service_records.get(service_key)
    if target_record is None:
        # The session must have been closed mid-flight: close-drain
        # zero'd out our in-flight arg contribution and released the
        # last holder. Deliver an error if the session is somehow
        # still present (it isn't, normally).
        _deliver_invoke_error(
            state,
            session_id,
            slot,
            f"service {service_key} no longer tracked",
        )
        return

    # ``resolved_args`` carries real ``ServiceRef``s for any
    # ``["callback", slot]`` references — the commit phase of
    # ``apply_mutation`` decoded them via ``unescape`` with a
    # session-bound ``lookup_callback`` that returns the bridged
    # send-side ref. Nothing to wait on here: callbacks are
    # guaranteed fully built by the time the invoke worker starts.

    # Open call_scope with the target service's scope plus every
    # arg-service / arg-entity scope included so FxtrIDs and the
    # target ref are reachable via the parent chain (per-record-scope
    # model — there is no global entities umbrella).
    try:
        target_scope = await _await_service_bridge_or_raise(target_record)
    except Exception as exc:
        _deliver_invoke_error(state, session_id, slot, _format_invoke_error(exc))
        return

    # Walk args + kwargs for ServiceRefs and entity FxtrIDs so we can
    # include their scopes in ``call_scope``. After the per-record-scope
    # refactor (stage 6a.2) there is no global entities umbrella —
    # each entity-typed arg's per-record scope is the only place the
    # arg is loadable through ``call_scope`` inheritance.
    arg_fids, arg_service_refs = _walk_refs([resolved_args, resolved_kwargs])
    arg_service_scopes: list[FxtrScope] = []
    arg_entity_scopes: list[FxtrScope] = []
    seen_arg_skeys: set[ServiceKey] = set()
    seen_arg_ekeys: set[ProvenancedKey] = set()
    try:
        for sref in arg_service_refs:
            sk = ServiceKey(handle=sref.handle, type_id=sref.service_type_id)
            if sk in seen_arg_skeys:
                continue
            seen_arg_skeys.add(sk)
            arg_record = state.service_records.get(sk)
            if arg_record is None:
                raise ValueError(
                    f"service {sk} no longer tracked (arg short_id resolved at "
                    "op-time but the record is gone now)"
                )
            arg_service_scopes.append(await _await_service_bridge_or_raise(arg_record))
        for fid in arg_fids:
            ek = provenanced_key_for(fid)
            if ek in seen_arg_ekeys:
                continue
            seen_arg_ekeys.add(ek)
            ent_record = state.entity_records.get(ek)
            if ent_record is None:
                raise ValueError(
                    f"entity {fid.id_string()} no longer tracked (arg short_id "
                    "resolved at op-time but the record is gone now)"
                )
            arg_entity_scopes.append(ent_record.scope)
    except Exception as exc:
        _deliver_invoke_error(state, session_id, slot, _format_invoke_error(exc))
        return

    async with FxtrScope(
        isolated=True,
        include=[target_scope, *arg_service_scopes, *arg_entity_scopes],
        name=f"invoke/slot-{slot}",
    ) as call_scope:
        try:
            target_ref = ServiceRef(
                handle=target_record.service_key.handle,
                target_type=UnknownService,
                service_type_id=target_record.service_key.type_id,
            )
            proxy = await target_ref.resolve(call_scope)
            method = getattr(proxy, method_name)
            result = await method(*resolved_args, **resolved_kwargs)
        except Exception as exc:
            _deliver_invoke_error(state, session_id, slot, _format_invoke_error(exc))
            return

        # Hand off to multi-phase delivery. ``deliver_invoke_result_to_slot``
        # awaits any newly-spawned service-record bridges before
        # returning, so by the time we reach the end of this ``async
        # with`` they're Independent and call_scope can drain freely.
        await deliver_invoke_result_to_slot(state, session_id, slot, result, call_scope)


def get_or_create_service_record(
    state: WorkstationState,
    ref: ServiceRef[Any],
    source_scope: FxtrScope,
) -> TrackedServiceRecord:
    """Sync primitive: get-or-create the :class:`TrackedServiceRecord`
    for ``ref``'s :class:`ServiceKey`.

    On first creation, spawns :func:`service_record_worker` on
    ``state.background_tg`` to bridge ``ref`` from ``source_scope``
    into the record's own ``service_scope``. Returns the record
    without awaiting :attr:`TrackedServiceRecord.bridge_ready` —
    callers that need the bridge to be up before proceeding (e.g.
    :meth:`WorkstationServiceImpl.register_service`, the 3c.3b
    invoke-result delivery worker) await it themselves.

    Sync (``start_soon`` is sync), so two concurrent callers that
    both try to register the same key are naturally serialized: one
    sees ``service_records.get(key)`` return ``None`` and creates +
    spawns; the other sees the existing record and returns it.

    Mirrors :func:`_get_or_create_entity_record` for the entity side
    (which is plumbing-only — entities don't have a per-record
    worker, so there's nothing to spawn).
    """
    service_key = ServiceKey(handle=ref.handle, type_id=ref.service_type_id)
    short_id = state.short_ids.get_or_assign_service(service_key)
    tracked = state.service_records.get(service_key)
    if tracked is not None:
        return tracked
    # New record. Touch before the dict insert so the buffer's
    # pre-image captures "no record". Caller is expected to be
    # inside a ``mutation_context``.
    state.touch("services", short_id)
    tracked = TrackedServiceRecord(service_key=service_key, short_id=short_id)
    state.service_records[service_key] = tracked
    # Reclaim the short-id mapping once the underlying handle dies *and*
    # the record is gone (guarded inside the callback) — this replaces
    # the allocator's old unconditional forget-on-token-end so a
    # broken-but-still-tracked service stays resolvable.
    ref.handle.token.register_end_callback(
        lambda: _on_service_handle_token_ended(state, service_key)
    )
    state.background_tg.start_soon(service_record_worker, state, tracked, ref, source_scope)
    return tracked


async def service_record_worker(
    state: WorkstationState,
    tracked: TrackedServiceRecord,
    source_ref: ServiceRef[Any],
    source_scope: FxtrScope,
) -> None:
    """Worker task that owns a :class:`TrackedServiceRecord`'s scope.

    Opens an ``isolated=True`` scope with
    ``include=[state.root_scope]``, bridges ``source_ref`` from
    ``source_scope`` into it, signals :attr:`bridge_ready`, and waits
    on :attr:`shutdown_event` before letting the scope CM drain.

    Errors during the bridge surface via
    :attr:`TrackedServiceRecord.bridge_error`; ``register_service``
    raises that to the caller.
    """
    try:
        async with FxtrScope(
            isolated=True,
            include=[state.root_scope],
            name=f"service/{tracked.short_id}",
        ) as service_scope:
            try:
                await service_scope.bridge_service_from_scope(
                    source_ref, source_scope, keepalive_via="independent_lease"
                )
            except Exception as exc:
                tracked.bridge_error = exc
                tracked.bridge_ready.set()
                return
            tracked.service_scope = service_scope
            # Observe the gating grant: register a broken-callback on
            # the bridge's standing keep-alive lease so we learn when
            # the source service's grant is revoked / its RPC
            # connection drops. The lease is read-only here — we must
            # not discard it (it backs the bridge for the scope's
            # lifetime). ``None`` only for non-independent bridge
            # shapes (pipelined / dependent), which the
            # ``independent_lease`` keepalive path above does not
            # produce; log if that ever changes.
            keepalive = service_scope._peek_independent_keepalive_lease(source_ref.handle)
            if keepalive is not None:
                keepalive.register_broken_callback(
                    lambda reason: _on_service_lease_broken(state, tracked, reason)
                )
            else:
                _log.warning(
                    "service %s bridged without an independent keep-alive lease; "
                    "break notifications disabled for it",
                    tracked.short_id,
                )
            tracked.bridge_ready.set()
            await tracked.shutdown_event.wait()
    finally:
        # If we exit before bridge_ready was set (e.g. cancellation
        # propagating up from the bridge or the shutdown wait), unblock
        # waiters on register_service. ``BaseException`` is *not*
        # caught above — cancellations / shutdown signals propagate
        # to the task group as required by structured concurrency,
        # but we still need to make sure register_service waiters
        # don't deadlock.
        if not tracked.bridge_ready.is_set():
            if tracked.bridge_error is None:
                tracked.bridge_error = RuntimeError("service worker exited before bridging")
            tracked.bridge_ready.set()


# --- Callback machinery ---------------------------------------------
#
# Callbacks are workstation-minted services that route ``notify``
# invocations into the originating session's event log. They share
# the existing service-record / bridge / refcount machinery; the
# only new ingredients are:
#
#  - :class:`CallbackRecord` (slot-side handle the session holds in
#    ``open_callbacks``);
#  - :func:`callback_record_worker` (manages the impl_scope and runs
#    the standard ``get_or_create_service_record`` flow);
#  - :func:`_finalize_callback_ready` / :func:`_finalize_callback_failure`
#    (sync transitions for the ``pending → ready`` and
#    ``pending → cancelled`` paths).
#
# Open-callback contributions ride the same ``service_refcounts``
# dict as explicit pins / retained event refs / in-flight delivery,
# so cleanup uniformly goes through the existing
# :func:`_incref_session_service` / :func:`_decref_session_service`
# / :func:`maybe_release_service_record` primitives.


async def callback_record_worker(
    state: WorkstationState,
    session_id: UUID,
    slot: int,
    *,
    task_status: anyio.abc.TaskStatus[CallbackRecord] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Background task per callback. Owns ``impl_scope`` and drives the
    receive-side lifecycle.

    Started via ``await task_group.start(...)`` so the caller (the
    pre-commit phase of :meth:`apply_mutation`) blocks until the
    callback is fully formed. The worker's setup steps:

    1. Open an isolated mountable ``impl_scope``.
    2. Mount a :class:`WorkstationInboxImpl` and resolve its ref.
    3. Run :func:`get_or_create_service_record` to mint the
       underlying :class:`TrackedServiceRecord` (which spawns its
       own ``service_record_worker`` to bridge).
    4. Await ``bridge_ready``. If the bridge errored, release the
       just-minted record and re-raise so
       ``task_group.start(...)`` propagates the error to the caller.
    5. Build the :class:`CallbackRecord` and signal readiness via
       ``task_status.started(callback_record)``.
    6. Park on ``shutdown_event`` until the underlying record is
       released by ``close_callback`` / session close.

    Lifetime story for ``impl_scope``: the bridge from ``impl_scope``
    into the record's ``service_scope`` pins ``impl_scope`` until the
    bridge drops, just like :meth:`WorkstationServiceImpl.register_service`
    pins its dispatch ``call_scope``. We keep the ``async with`` open
    until ``shutdown_event`` fires (refcount→0) so the bridge is held
    for the receive side's full lifetime; ``impl_scope`` then drains
    naturally on exit.
    """
    async with FxtrScope(
        isolated=True,
        include=[state.root_scope],
        name=f"callback_impl/{session_id}/{slot}",
    ) as impl_scope:
        impl = WorkstationInboxImpl(state, session_id, slot, impl_scope=impl_scope)
        impl_ref = await impl_scope.mount(impl)
        with state.mutation_context():
            tracked = get_or_create_service_record(state, impl_ref, impl_scope)
        await tracked.bridge_ready.wait()
        if tracked.bridge_error is not None:
            # Release the just-minted record (no holders — caller
            # hasn't been signalled yet, so nothing has incref'd).
            _log.error(
                "callback bridge failed for session %s slot %d: %r",
                session_id,
                slot,
                tracked.bridge_error,
            )
            with state.mutation_context():
                maybe_release_service_record(state, tracked)
            raise tracked.bridge_error
        callback_record = CallbackRecord(
            slot=slot,
            impl=impl,
            tracked=tracked,
            service_key=tracked.service_key,
            service_short_id=tracked.short_id,
        )
        task_status.started(callback_record)
        await tracked.shutdown_event.wait()


async def session_worker(
    state: WorkstationState,
    session_id: UUID,
    receive_stream: anyio.streams.memory.MemoryObjectReceiveStream[_PendingMutation],
) -> None:
    """Drain a session's mutation queue sequentially.

    Spawned per session at :meth:`WorkstationState.create_session`
    time on :attr:`background_tg`. Loops over the queue and calls
    :meth:`apply_mutation` for each request. The send half of the
    queue lives in :attr:`session_mutation_queues`; closing the send
    half (CloseOp commit, server shutdown) makes the next
    ``receive`` raise :class:`anyio.EndOfStream`, which exits the
    worker.

    Mutations on the same session are serialized through this loop;
    mutations on different sessions run concurrently on their
    respective workers.
    """
    async with receive_stream:
        async for pending in receive_stream:
            await state.apply_mutation(
                session_id,
                mutator_connection=pending.mutator_connection,
                mutator_subscription_id=pending.mutator_subscription_id,
                request_id=pending.request_id,
                ack_seq=pending.ack_seq,
                update_id=pending.update_id,
                patch=pending.patch,
                ops=pending.ops,
            )
