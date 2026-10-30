"""Subscribe/unsubscribe protocol primitives for event-emitting services.

Three small ``@service_protocol`` Protocols — :class:`Subscriber`,
:class:`UnsubscribeHandle`, :class:`EventSource` — that together define
fxtr2's standard explicit-subscription contract:

- An :class:`EventSource` accepts a :class:`Subscriber` ref via
  :meth:`EventSource.subscribe` and returns an
  :class:`UnsubscribeHandle` ref.
- Anyone who subscribes for a duration is **required** to call
  :meth:`UnsubscribeHandle.unsubscribe` when done (e.g. in a
  ``finally`` block). The unsubscribe doubles as the release signal:
  it lets the source drop its bridge to the subscriber so both sides
  can clean up promptly, instead of the source only discovering the
  loss on its next delivery attempt.
- As a crash backstop, sources must additionally drop any subscription
  whose :meth:`Subscriber.on_event` raises — that's how a source reaps
  peers that vanished without unsubscribing.

For implementing the source side, see
:class:`fxtr2.services.subscription_util.SubscriberHub` — a reusable
broadcast helper that owns the per-subscription worker / scope / bridge
machinery and mounts the unsubscribe-handle impl.

**Payload typing.** ``Subscriber[T]`` / ``EventSource[T]`` deliberately
leave ``T`` unbounded, with the documented contract that **T must
serialize to** :data:`~fxtr2.services.service_cap_common.FxtrServiceData`
under the service dialect in play. Wire conversion is asymmetric — the
sender's annotation drives encode and the receiver impl's annotation
drives decode — so a *typed* subscriber (``T`` = some dataclass) can
coherently receive events from a source that publishes pre-encoded
wire dicts (``T = FxtrServiceData``): the dicts decode into the
receiver's dataclasses via its own annotation.
:class:`~fxtr2.services.subscription_util.SubscriberHub` keeps its
``FxtrServiceData`` bound — it genuinely stores wire values.

**Type-id policy.** This module pre-declares **no** service type ids.
Type identity belongs to impls, which define their own id (and
namespace) reflecting what they actually support
(:mod:`fxtr2.services.subscription_util` does exactly that for the
unsubscribe-handle impl it ships).
"""

from __future__ import annotations

from typing import Protocol

from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr2.services.typed_service import service_protocol

__all__ = [
    "EventSource",
    "Subscriber",
    "UnsubscribeHandle",
]


@service_protocol(allow_protocol_subtypes=True)
class Subscriber[T](Protocol):
    """The receive ("send-to") side of a subscription.

    A subscriber is handed to :meth:`EventSource.subscribe` and then
    receives events via :meth:`on_event` until the subscription ends —
    either because the holder of the matching :class:`UnsubscribeHandle`
    called :meth:`~UnsubscribeHandle.unsubscribe`, or because an
    :meth:`on_event` dispatch raised and the source reaped the
    subscription (the crash backstop).

    ``T`` is unbounded but must serialize to
    :data:`~fxtr2.services.service_cap_common.FxtrServiceData` (see
    the module docstring's *Payload typing* note).
    """

    async def on_event(self, payload: T) -> None:
        """Deliver one event to the subscriber.

        There is no return value — and deliberately no in-band "stop
        sending" signal. A subscriber that wants to stop receiving
        events calls :meth:`UnsubscribeHandle.unsubscribe` on the
        handle returned at subscribe time. An exception raised here is
        treated by sources as "the subscriber is gone": the
        subscription is dropped.

        Args:
            payload: The event data.
        """
        ...


@service_protocol
class UnsubscribeHandle(Protocol):
    """A capability for ending an in-progress subscription.

    Returned by :meth:`EventSource.subscribe`. Holders that subscribed
    for a duration are required to call :meth:`unsubscribe` when done
    (e.g. in a ``finally`` block).
    """

    async def unsubscribe(self) -> None:
        """End the subscription and release the source's hold on the
        subscriber.

        Callers should treat this as best-effort: a subscription that
        was already torn down on the source side (e.g. reaped via the
        crash backstop, or the source itself shut down) surfaces as a
        transport error, which callers interpret as "already gone".
        Guarding the call with a timeout (``anyio.move_on_after``) is
        recommended when unsubscribing from cleanup paths.
        """
        ...


@service_protocol(allow_protocol_subtypes=True)
class EventSource[T](Protocol):
    """An object that emits events that can be subscribed to.

    Implementor obligations:

    - Deliver subsequent events to every live subscriber until its
      :class:`UnsubscribeHandle` is invoked.
    - Drop a subscription whose :meth:`Subscriber.on_event` raises
      (crash backstop for peers that vanished without unsubscribing).

    Note for protocol authors: a Protocol that wants to *be* an event
    source should **subclass** ``EventSource`` (the base is marked
    ``allow_protocol_subtypes=True``)::

        @service_protocol
        class MyProto(EventSource[MyEvent], Protocol):
            async def my_other_method(self) -> None: ...

    ``subscribe`` is inherited with the subclass's ``T`` binding
    substituted into the typed param. Re-declaring ``subscribe`` on
    the subclass purely to attach a richer docstring is fine (an
    override's own annotations win). Wire dispatch is by method name,
    so refs re-typed to ``EventSource`` keep dispatching correctly
    either way.

    ``T`` is unbounded but must serialize to
    :data:`~fxtr2.services.service_cap_common.FxtrServiceData` (see
    the module docstring's *Payload typing* note).
    """

    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[T]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe ``subscriber`` to this source's events.

        Args:
            subscriber: A service that should receive events until the
                returned unsubscribe handle is invoked.

        Returns:
            A handle which the caller uses to unsubscribe when done.
            The caller is obligated to invoke it (best-effort, e.g. in
            a ``finally`` block) — unsubscribing is also the release
            signal that lets the source drop its bridge to
            ``subscriber``.
        """
        ...
