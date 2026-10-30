"""``SubscriberHub`` — reusable broadcast helper for ``EventSource`` impls.

An :class:`~fxtr2.services.subscription_types.EventSource` implementor
holds one :class:`SubscriberHub` and delegates to it:

.. code-block:: python

    class MySourceImpl(TypedServiceImpl[EventSource[MyPayload]]):
        def __init__(self, impl_tg: anyio.abc.TaskGroup):
            super().__init__()
            self._hub = SubscriberHub[MyPayload](task_group=impl_tg, mode="coalesce")

        @service_method
        async def subscribe(
            self, subscriber: ServiceRef[Subscriber[MyPayload]]
        ) -> ServiceRef[UnsubscribeHandle]:
            return await self._hub.subscribe(subscriber, get_active_scope())

    # ...and from the owning code, whenever something happens (any
    # context authorized for the payload's origin scope):
    impl._hub.publish(new_value)              # origin = active scope
    # ...and at source teardown (optionally after publishing a
    # domain-level close message with origin_scope=None):
    await impl._hub.aclose()

The hub owns the per-subscription machinery: per subscription, a
**worker task** (isolated ``sub_scope`` holding the subscriber's
bridge; delivers events) and a **holder task** (isolated ``hold_scope``
hosting the mounted
:class:`~fxtr2.services.subscription_types.UnsubscribeHandle` impl,
whose natural drain doubles as handle-drop detection), both spawned on
the caller-supplied task group.

**Events carry their own residency.** The hub has no scope of its own.
``publish`` captures a lease on the payload's *origin* scope (default:
the active scope — e.g. the dispatch call scope the payload was stored
in); each pending delivery holds its own clone. Delivery opens a
transient per-event scope parented on ``{sub_scope, origin}`` —
consuming the lease — so payload entities stay loadable even after the
origin drained, with zero copies. The lease is held only until the
event is delivered or superseded (coalesce mode self-bounds; a slow
queue-mode subscriber holds its pending events' origin scopes
drained-but-open until it catches up — every worker exit path discards
whatever is still pending). ``origin_scope=None`` publishes with no
lease, for payloads carrying no ``FxtrID``\\ s (notifications, close
messages) — publishing an id-bearing payload without an origin is a
caller bug that surfaces as a delivery encode error.

**Subscriptions pin nothing of the source's.** Both per-subscription
scopes are isolated (``include=[GLOBAL_SCOPE]`` only, for type
descriptors), so live subscriptions leave the source's mount token
unpinned: a source whose real holders are gone can drain and tear down
even with subscriptions outstanding. The contract is: *hold the source
ref to keep the source; hold your* ``UnsubscribeHandle`` *to keep your
subscription. Neither implies the other.*

**Handle-drop is a teardown signal.** The peer pins ``hold_scope`` via
its handle; the holder task parks in ``hold_scope``'s natural drain
after setup, so the drain completing — the peer released the handle
(politely, after ``unsubscribe``) or died (its leases broke) — tears
the subscription down without waiting for a failed delivery.

**Teardown is forceful.** When a subscription ends — unsubscribe,
handle drop, a delivery failure, or task-group cancel — ``sub_scope``
is torn down forcefully rather than gracefully drained: it's the
durable "you're done" signal (a peer that missed the unsubscribe gets
a dispatch error on its next use), and it avoids parking forever on a
peer that never releases its subscriber bridge. A delivery failure
(``on_event`` raised, or the pending event's origin lease broke) kills
the whole subscription — the crash backstop — rather than skipping the
event: skipping could deliver later events out of expected order, and
a subscriber can always resubscribe for a fresh view.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections import deque
from collections.abc import Sequence
from typing import Any, Literal, Protocol, cast

import anyio
import anyio.abc

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    FxtrScope,
    ServiceKey,
    ServiceRef,
    get_active_scope,
)
from fxtr2.leases.lease_types import Lease
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

__all__ = [
    "HUB_UNSUBSCRIBE_HANDLE_TYPE_ID",
    "SUBSCRIPTION_UTIL_NS",
    "DeliveryMode",
    "SubscriberHub",
]

_log = logging.getLogger(__name__)

#: Stable namespace UUID for types defined by this module. Don't change.
SUBSCRIPTION_UTIL_NS = uuid.UUID("74222daf-c325-4564-896b-b6a82c00df05")

#: Runtime type identity of the unsubscribe-handle impl the hub mounts
#: per subscription. (The protocols in ``subscription_types`` declare
#: no ids — type identity belongs to impls.)
HUB_UNSUBSCRIBE_HANDLE_TYPE_ID = system_entities.define_type(
    SUBSCRIPTION_UTIL_NS, "hub_unsubscribe_handle"
)

#: Per-hub delivery semantics — see :class:`SubscriberHub`.
type DeliveryMode = Literal["coalesce", "queue"]


class _ActiveScopeDefault:
    """Sentinel type for "default to the active scope at call time"."""

    def __repr__(self) -> str:
        return "<active scope at call time>"


_ACTIVE_SCOPE = _ActiveScopeDefault()


@dataclasses.dataclass
class _PendingEvent:
    """One undelivered value plus the residency it rides on.

    ``lease`` is a (per-subscription, per-event) clone of the
    publisher's authorization for ``origin_scope``; it is *consumed*
    into the per-delivery scope at delivery time, or discarded when the
    event is superseded (coalesce) or the worker exits with it still
    pending. ``origin_scope`` / ``lease`` are ``None`` together
    (entity-free payloads).
    """

    value: object
    origin_scope: FxtrScope | None
    lease: Lease[Any] | None

    def discard(self) -> None:
        if self.lease is not None:
            self.lease.discard()
            self.lease = None


class _DeliveryStrategy(Protocol):
    """Per-subscription pending-delivery state — the mode-specific half
    of the delivery loop.

    One instance exists per subscription. The hub side calls
    :meth:`push` (from ``publish`` and ``subscribe``'s seed); the
    subscription's worker calls :meth:`take` once per loop iteration
    and delivers whatever it returns, parking on the subscription's
    wake event whenever it returns ``None``. Both methods are sync and
    never block, so the worker loop stays mode-agnostic and no locking
    is needed (all access happens on one event loop, with no awaits in
    between). :meth:`drain` discards everything still pending (origin
    leases included) — called on every worker exit path.
    """

    def push(self, event: _PendingEvent) -> None:
        """Record ``event`` as pending delivery.

        How it combines with previously-pushed undelivered events is
        the mode's choice (supersede them, queue behind them, ...); a
        superseded event is discarded (releasing its origin lease).
        """
        ...

    def take(self) -> _PendingEvent | None:
        """Remove and return the next event to deliver, or ``None``
        when nothing is pending.

        Each pushed event is returned at most once; what "next" means
        (newest-wins vs. FIFO) is the mode's choice. Ownership of the
        returned event's lease moves to the caller.
        """
        ...

    def drain(self) -> None:
        """Discard every pending event (and its origin lease)."""
        ...


class _CoalesceDelivery:
    """Latest-value :class:`_DeliveryStrategy`: ``push`` overwrites any
    undelivered predecessor (discarding its origin lease), so multiple
    publishes during one in-flight ``on_event`` collapse to one
    delivery of the newest value."""

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending: _PendingEvent | None = None

    def push(self, event: _PendingEvent) -> None:
        if self._pending is not None:
            self._pending.discard()
        self._pending = event

    def take(self) -> _PendingEvent | None:
        event = self._pending
        self._pending = None
        return event

    def drain(self) -> None:
        if self._pending is not None:
            self._pending.discard()
            self._pending = None


class _QueueDelivery:
    """Every-event :class:`_DeliveryStrategy`: undelivered events are
    queued and ``take``\\ n in publish order.

    .. todo::

       Unbounded in v1. Decide a bounds / backpressure policy
       (drop-oldest vs. block the publisher vs. kill the subscription)
       before pointing a high-rate source at slow subscribers. Note a
       slow subscriber also pins every pending event's origin scope
       (drained-but-open) until it catches up.
    """

    __slots__ = ("_queue",)

    def __init__(self) -> None:
        self._queue: deque[_PendingEvent] = deque()

    def push(self, event: _PendingEvent) -> None:
        self._queue.append(event)

    def take(self) -> _PendingEvent | None:
        if not self._queue:
            return None
        return self._queue.popleft()

    def drain(self) -> None:
        while self._queue:
            self._queue.popleft().discard()


@dataclasses.dataclass
class _Subscription:
    """One subscriber's per-subscription state — shared between the hub
    and the subscription's worker + holder tasks.

    The ``wake`` event is *replaced* by the worker after each consumed
    wait, so a publish landing during an in-flight ``on_event`` fires
    the fresh event, not the one the worker already consumed.
    """

    key: ServiceKey
    """:class:`ServiceKey` of the subscriber ref. Informational only
    (log messages) — subscriptions are not deduplicated by it."""

    delivery: _DeliveryStrategy
    """Mode-specific pending-delivery state. The worker loop is
    mode-agnostic; it just ``take()``\\ s."""

    wake: anyio.Event
    """Fired by :meth:`SubscriberHub.publish`, by
    :meth:`request_unsubscribe`, and by :meth:`request_flush`."""

    worker_exited: anyio.Event
    """Set by the worker task on exit (after teardown).
    :meth:`SubscriberHub.aclose` awaits it."""

    holder_exited: anyio.Event
    """Set by the holder task on exit (or by ``subscribe``'s failure
    cleanup when the holder never ran). :meth:`SubscriberHub.aclose`
    awaits it."""

    unsubscribe_requested: bool = False
    """Set (sync, idempotent) when the unsubscribe handle fires, when
    the holder's drain completes (handle dropped), or on subscribe
    failure; the worker checks it before every delivery and exits,
    dropping anything still pending."""

    flush_requested: bool = False
    """Set by :meth:`SubscriberHub.aclose`: the worker delivers
    everything pending, then exits once the strategy is empty."""

    done: bool = False
    """Set by the worker on exit. ``done`` entries whose holder has
    also exited are reaped on the next :meth:`SubscriberHub.publish`."""

    holder_cancel: anyio.CancelScope | None = None
    """The holder task's cancel scope, assigned by the holder before
    any awaits. Cancelling it force-closes ``hold_scope`` (breaking the
    peer's handle — the durable teardown signal)."""

    holder_cancel_requested: bool = False
    """Latches :meth:`cancel_holder` calls that land before the holder
    has assigned its cancel scope."""

    def request_unsubscribe(self) -> None:
        self.unsubscribe_requested = True
        self.wake.set()

    def request_flush(self) -> None:
        self.flush_requested = True
        self.wake.set()

    def cancel_holder(self) -> None:
        self.holder_cancel_requested = True
        if self.holder_cancel is not None:
            self.holder_cancel.cancel()


class _HubUnsubscribeHandleImpl(TypedServiceImpl[UnsubscribeHandle]):
    """The unsubscribe handle the hub mounts in each subscription's
    ``hold_scope``. The peer's hold on this handle *is* the
    subscription's keepalive: releasing it (or dying) completes the
    holder's drain, which tears the subscription down."""

    def __init__(self, entry: _Subscription) -> None:
        super().__init__()
        self._entry = entry

    def _target_type_annotation(self) -> Any:
        return UnsubscribeHandle

    def _service_type_id(self) -> FxtrID:
        return HUB_UNSUBSCRIBE_HANDLE_TYPE_ID

    @service_method
    async def unsubscribe(self) -> None:
        """Flag the subscription for teardown and wake its worker.

        Sync flag-flip + event-set; idempotent (calling it again while
        still holding the handle is a no-op). The worker does the
        actual teardown; the reply rides ``hold_scope``, which the
        caller still pins while awaiting it, so the reply is never
        raced by the teardown."""
        self._entry.request_unsubscribe()


class SubscriberHub[T: FxtrServiceData]:
    """Broadcast helper that owns the source side of
    :class:`~fxtr2.services.subscription_types.EventSource`
    subscriptions.

    The hub has no scope of its own — events carry their residency via
    origin-scope leases (see the module docstring), and the
    per-subscription scopes are isolated, so subscriptions never pin
    the source's mount scope.

    Args:
        task_group: Caller-owned task group the per-subscription worker
            and holder tasks are spawned on. Cancelling it tears down
            every subscription (forcefully) — that is the bounded
            alternative to :meth:`aclose`'s unbounded flush.
        mode: Per-hub delivery semantics, uniform across subscribers:

            - ``"coalesce"`` — latest-value, skip-ahead: multiple
              publishes during one in-flight ``on_event`` collapse to
              one delivery of the newest value. Right for idempotent /
              self-superseding payloads (reload notifications, chain
              heads).
            - ``"queue"`` — every-event: each subscription holds a
              FIFO of undelivered values, drained in order. Right for
              stream semantics where every event matters. Unbounded in
              v1 — see the bounds/backpressure TODO on the strategy.
    """

    def __init__(
        self,
        *,
        task_group: anyio.abc.TaskGroup,
        mode: DeliveryMode = "coalesce",
    ) -> None:
        if mode not in ("coalesce", "queue"):
            raise ValueError(f"Unknown delivery mode: {mode!r}")
        self._task_group = task_group
        self._mode: DeliveryMode = mode
        # Mutated by `subscribe` (append) and by `publish` (reap of
        # fully-exited entries).
        self._subscriptions: list[_Subscription] = []
        self._closing = False

    @property
    def live_subscription_count(self) -> int:
        """Number of subscriptions whose worker hasn't exited yet."""
        return sum(1 for entry in self._subscriptions if not entry.done)

    def _make_delivery(self) -> _DeliveryStrategy:
        return _CoalesceDelivery() if self._mode == "coalesce" else _QueueDelivery()

    def _check_not_closing(self, method: str) -> None:
        if self._closing:
            raise RuntimeError(f"SubscriberHub.{method} called after aclose() began")

    @staticmethod
    def _resolve_origin(
        origin_scope: FxtrScope | None | _ActiveScopeDefault,
    ) -> tuple[FxtrScope, Lease[Any]] | tuple[None, None]:
        """Resolve the origin default and capture one authorization
        clone for it (the per-event leases are cloned off this one).

        Raises if the resolved origin scope is not authorized in the
        calling context — a draining origin needs an explicit
        ``with origin.authorize(lease.clone()):`` around the call.
        """
        origin = (
            get_active_scope() if isinstance(origin_scope, _ActiveScopeDefault) else origin_scope
        )
        if origin is None:
            return None, None
        return origin, origin.clone_authorization()

    def publish(
        self,
        value: T,
        *,
        origin_scope: FxtrScope | None | _ActiveScopeDefault = _ACTIVE_SCOPE,
    ) -> None:
        """Record ``value`` as pending for every live subscription and
        wake their workers.

        Sync — no awaits. Reaps fully-exited entries. In ``"coalesce"``
        mode a value published while a delivery is in flight supersedes
        any undelivered predecessor (releasing its origin lease); in
        ``"queue"`` mode it appends.

        Args:
            value: The payload to broadcast.
            origin_scope: The scope ``value``'s entities live in
                (= are reachable from). Defaults to the active scope at
                call time — right for a dispatch publishing a payload
                stored in its own call scope. Pass an explicit scope
                when the entities live elsewhere, or ``None`` for
                payloads carrying no ``FxtrID``\\ s at all (close
                messages etc. — usable from unauthorized contexts such
                as post-drain lifespan teardown). With an origin, the
                calling context must be authorized for it; the hub
                captures one lease clone per live subscription, held
                until that subscription delivers or supersedes the
                event.

        Raises:
            RuntimeError: :meth:`aclose` has begun, or ``origin_scope``
                resolved to a scope this context is not authorized for.
        """
        self._check_not_closing("publish")
        origin, base_lease = self._resolve_origin(origin_scope)
        try:
            if any(e.done and e.holder_exited.is_set() for e in self._subscriptions):
                self._subscriptions = [
                    e for e in self._subscriptions if not (e.done and e.holder_exited.is_set())
                ]
            for entry in self._subscriptions:
                if entry.done:
                    continue
                lease = base_lease.clone() if base_lease is not None else None
                entry.delivery.push(_PendingEvent(value=value, origin_scope=origin, lease=lease))
                entry.wake.set()
        finally:
            if base_lease is not None:
                base_lease.discard()

    async def subscribe(
        self,
        subscriber: ServiceRef[Subscriber[T]],
        source_scope: FxtrScope,
        *,
        seed: Sequence[T] | None = None,
        seed_origin: FxtrScope | None | _ActiveScopeDefault = _ACTIVE_SCOPE,
    ) -> ServiceRef[UnsubscribeHandle]:
        """Register ``subscriber`` and return its unsubscribe handle.

        Spawns the subscription's worker + holder tasks on the hub's
        task group; this method returns only once the subscription is
        fully established (``sub_scope`` open, subscriber bridged,
        handle mounted + bridged into ``source_scope``) — failures
        before that point propagate to the caller and nothing is
        retained.

        **The returned handle is the subscription's keepalive**: the
        subscription lives until the holder either invokes
        ``unsubscribe`` or releases the handle (drops its last lease /
        dies) — whichever comes first. It does *not* keep the source
        alive (hold the source ref for that).

        Args:
            subscriber: The subscriber ref, reachable from
                ``source_scope``.
            source_scope: The scope ``subscriber`` is bridged *from*
                and the handle ref is bridged *into* — in a
                service-dispatch context, the dispatch call scope
                (``get_active_scope()``).
            seed: Values delivered to this subscription first, in
                order, before anything published after this call (in
                ``"coalesce"`` mode later seeds/publishes supersede
                earlier ones as usual). For e.g. a state snapshot
                preceding the live stream. *Recommendation for a
                gapless snapshot→stream handoff: don't await between
                computing the seed and calling ``subscribe``* —
                seeding and registration share one no-await window, so
                a concurrent publish can neither be missed nor
                duplicated.
            seed_origin: Like ``publish``'s ``origin_scope``, for the
                seed values (default: active scope; ``None`` for
                entity-free seeds). A caller whose origin scope is
                draining wraps the call:
                ``with run_scope.authorize(lease.clone()): await
                hub.subscribe(..., seed_origin=run_scope)``.

        Subscriptions are **not deduplicated**: every call creates an
        independent subscription with its own handle, even for a
        subscriber that's already subscribed — the contract is simply
        "delivered to until its handle is unsubscribed or dropped", so
        a duplicate subscribe duplicates what the subscriber receives.

        Raises:
            RuntimeError: :meth:`aclose` has begun, or ``seed_origin``
                resolved to a scope this context is not authorized for.
        """
        self._check_not_closing("subscribe")
        key = ServiceKey(handle=subscriber.handle, type_id=subscriber.service_type_id)
        entry = _Subscription(
            key=key,
            delivery=self._make_delivery(),
            wake=anyio.Event(),
            worker_exited=anyio.Event(),
            holder_exited=anyio.Event(),
        )
        if seed:
            origin, base_lease = self._resolve_origin(seed_origin)
            try:
                for value in seed:
                    lease = base_lease.clone() if base_lease is not None else None
                    entry.delivery.push(
                        _PendingEvent(value=value, origin_scope=origin, lease=lease)
                    )
            finally:
                if base_lease is not None:
                    base_lease.discard()
        # No awaits between the seed pushes above and this append —
        # that's the no-dup-no-miss window: a publish racing in after
        # the append simply follows (queue) / supersedes (coalesce) the
        # seeds.
        self._subscriptions.append(entry)
        try:
            await self._task_group.start(self._subscription_worker, entry, subscriber, source_scope)
            handle_ref: ServiceRef[UnsubscribeHandle] = await self._task_group.start(
                self._handle_holder, entry, source_scope
            )
        except BaseException:
            # Worker or holder failed before signaling `started` (or
            # the holder failed after the worker came up). Tear down
            # whatever did start and un-register the entry.
            entry.request_unsubscribe()
            entry.cancel_holder()
            if entry.holder_cancel is None:
                # The holder never ran; nothing will set its event.
                entry.holder_exited.set()
            try:
                self._subscriptions.remove(entry)
            except ValueError:
                pass
            raise
        return handle_ref

    async def aclose(self) -> None:
        """Flush every live subscription, then tear everything down.

        Wakes all workers and waits — **unbounded** — for each live
        subscription's pending events to be delivered (strategy empty
        and the in-flight ``on_event`` returned), then cancels every
        holder (force-closing the ``hold_scope``\\ s, which breaks the
        peers' handles — the durable "source is gone" signal) and waits
        for all tasks to exit. ``publish`` / ``subscribe`` raise once
        this has begun.

        This is what the owning lifespan calls in its teardown /
        ``finally`` — typically right after publishing a domain-level
        close message with ``origin_scope=None`` (which works even from
        an unauthorized post-drain teardown context). There is
        deliberately no flush timeout: for a bounded / forceful close,
        cancel the owning task group instead — cancellation already
        force-closes every subscription.
        """
        self._closing = True
        entries = list(self._subscriptions)
        for entry in entries:
            entry.request_flush()
        for entry in entries:
            await entry.worker_exited.wait()
        for entry in entries:
            entry.cancel_holder()
        for entry in entries:
            await entry.holder_exited.wait()
        self._subscriptions = []

    async def _subscription_worker(
        self,
        entry: _Subscription,
        subscriber: ServiceRef[Subscriber[T]],
        source_scope: FxtrScope,
        *,
        task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        """Per-subscription worker. Owns ``sub_scope`` and the delivery
        loop.

        Lifecycle:

        1. Open ``sub_scope`` — isolated (``include=[GLOBAL_SCOPE]``
           only, so the subscription pins nothing of the source's).
        2. Bridge ``subscriber`` in from ``source_scope`` (independent
           lease on the subscriber ref's own token — the standing
           delivery route), signal readiness.
        3. Loop: exit if unsubscribe was requested (handle fired or
           dropped; pending events are discarded); otherwise take the
           next pending event and deliver it (parking on ``entry.wake``
           when there is none, replacing the event after each consumed
           wait); on flush (hub ``aclose``) exit once nothing is
           pending.
        4. Exit paths: unsubscribe/drop (holder is left to its natural
           drain), flush, delivery failure (crash backstop — also
           cancels the holder so the peer's handle breaks), task-group
           cancel. The ``force_close`` in the inner ``finally`` makes
           every path forceful — see the module docstring for why.
        """
        try:
            async with FxtrScope(
                isolated=True,
                include=[GLOBAL_SCOPE],
                name="subscriber_hub_subscription",
            ) as sub_scope:
                try:
                    await sub_scope.bridge_service_from_scope(
                        subscriber, source_scope, keepalive_via="independent_lease"
                    )
                    task_status.started()
                    while True:
                        if entry.unsubscribe_requested:
                            return
                        event = entry.delivery.take()
                        if event is None:
                            if entry.flush_requested:
                                return
                            await entry.wake.wait()
                            # Replace the event *after* the consumed
                            # wait so a publish landing during the next
                            # `on_event` fires the fresh event.
                            entry.wake = anyio.Event()
                            continue
                        try:
                            await self._deliver_one(sub_scope, subscriber, event)
                        except Exception:
                            # Crash backstop: any delivery failure
                            # (transport drop, remote raise, broken
                            # origin lease) ends the subscription —
                            # but not its siblings. Cancelling the
                            # holder breaks the peer's handle, so the
                            # peer can observe the teardown.
                            _log.warning(
                                "SubscriberHub: delivery failed; dropping "
                                "subscription for key=%r",
                                entry.key,
                                exc_info=True,
                            )
                            entry.cancel_holder()
                            return
                finally:
                    # Forceful teardown on every exit path.
                    sub_scope.force_close()
        finally:
            entry.delivery.drain()  # release pending origin leases
            entry.done = True
            entry.worker_exited.set()

    async def _deliver_one(
        self,
        sub_scope: FxtrScope,
        subscriber: ServiceRef[Subscriber[T]],
        event: _PendingEvent,
    ) -> None:
        """Deliver one event through a transient per-event scope.

        The scope is parented on ``sub_scope`` (the subscriber bridge +
        type descriptors) plus, when the event has one, its origin
        scope — entered via ``open_with_explicit_leases``, which is the
        one path allowed to derive from a draining-but-open origin and
        which *consumes* the event's captured lease, so the origin
        claim's lifetime becomes the delivery scope's lifetime. The
        subscriber proxy is resolved into that scope (re-typed to
        ``Subscriber[FxtrServiceData]`` — the hub treats payloads as
        untyped wire data) and ``on_event`` dispatched.

        One scope open + proxy resolve per delivery — fine at current
        rates; per-(subscription, origin) proxy caching is the future
        optimization if a hot source shows up.
        """
        try:
            include = [sub_scope]
            if event.origin_scope is not None:
                include.append(event.origin_scope)
            delivery_scope = FxtrScope(
                isolated=True, include=include, name="subscriber_hub_delivery"
            )
            parent_leases: dict[FxtrScope, Lease[Any]] = {
                sub_scope: sub_scope.clone_authorization()
            }
        except BaseException:
            event.discard()
            raise
        if event.origin_scope is not None:
            assert event.lease is not None
            parent_leases[event.origin_scope] = event.lease
            event.lease = None  # consumed below
        async with delivery_scope.open_with_explicit_leases(parent_leases=parent_leases):
            proxy = await subscriber.replace_service_type(Subscriber[FxtrServiceData]).resolve(
                delivery_scope
            )
            await proxy.on_event(cast(FxtrServiceData, event.value))

    async def _handle_holder(
        self,
        entry: _Subscription,
        source_scope: FxtrScope,
        *,
        task_status: anyio.abc.TaskStatus[ServiceRef[UnsubscribeHandle]] = (
            anyio.TASK_STATUS_IGNORED
        ),
    ) -> None:
        """Per-subscription holder. Owns ``hold_scope`` — whose natural
        drain *is* handle-drop detection.

        Setup: open the isolated ``hold_scope``, mount the unsubscribe
        handle, bridge it out into ``source_scope`` (while the base
        authorization is still live), signal readiness with the
        bridged ref. The CM body then ends at once: ``aclose`` drops
        the base and parks in the token-drain wait, leaving the scope
        draining-but-open for exactly as long as the peer holds the
        handle (inbound ``unsubscribe`` dispatches still land via the
        wire-lease derived path). The drain completing — handle
        released politely, or the peer died — falls through to
        ``request_unsubscribe()``, which is idempotent: it's the
        drop-fallback after an explicit unsubscribe *and* the
        post-cancel no-op after a crash-backstop / ``aclose`` cancel
        (where ``force_close`` broke the peer's handle instead).
        """
        try:
            with anyio.CancelScope() as cancel_scope:
                entry.holder_cancel = cancel_scope
                if entry.holder_cancel_requested:
                    cancel_scope.cancel()
                async with FxtrScope(
                    isolated=True,
                    include=[GLOBAL_SCOPE],
                    name="subscriber_hub_handle_hold",
                ) as hold_scope:
                    handle_ref = (
                        await hold_scope.mount(_HubUnsubscribeHandleImpl(entry))
                    ).replace_service_type(UnsubscribeHandle)
                    # Bridge out here, while hold_scope's base is still
                    # live; the peer's hold then pins hold_scope's own
                    # token, not the (promptly-draining) source_scope.
                    await source_scope.bridge_service_from_scope(
                        handle_ref, hold_scope, keepalive_via="independent_lease"
                    )
                    task_status.started(handle_ref)
                # CM body ends at once — see the docstring.
            entry.request_unsubscribe()
        finally:
            entry.holder_exited.set()
