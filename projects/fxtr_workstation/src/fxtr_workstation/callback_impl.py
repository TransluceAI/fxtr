"""Workstation-minted session-event-inbox implementation.

Each session-owned callback slot is an instance of
:class:`WorkstationInboxImpl` (a :class:`TypedServiceImpl` over
:class:`~fxtr_workstation.protocols.WorkstationSessionEventInbox`)
mounted on a workstation-owned scope. RPC peers that hold a ref to the
inbox either push payloads directly via ``notify`` or hand the inbox an
:class:`~fxtr2.services.subscription_types.EventSource` via
``subscribe_to`` — either way, events land in the originating session's
event log as :class:`~fxtr_workstation.client_protocol.CallbackEvent`\\ s.

``subscribe_to`` subscriptions are owned end-to-end by per-subscription
**parker tasks** on the workstation's ``background_tg`` (mirroring
``callback_record_worker``): each parker opens an isolated subscription
scope, bridges the source in, mounts an internal
:class:`_InboxSubscriberImpl`, subscribes, and parks until the inbox
closes — at which point it unsubscribes from the source (best-effort,
bounded by :data:`UNSUBSCRIBE_TIMEOUT_SECONDS`) and tears the
subscription scope down **forcefully**. Forceful teardown is
load-bearing twice over: (a) it's the durable "you're done" signal — a
source that missed the unsubscribe gets a dispatch error on its next
``on_event`` and reaps via its crash backstop; and (b) it breaks the
cross-pin cycle between the two sides' per-subscription scopes (the
source's scope holds a bridge to our subscriber; ours holds a bridge to
its unsubscribe handle), which a graceful drain could park on forever.

The companion :class:`~fxtr_workstation.state.CallbackRecord` (the
slot-side handle a session holds in ``open_callbacks``) lives
alongside the rest of the session bookkeeping in :mod:`.state`.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import anyio
import anyio.abc
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceKey, ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.service_dialect import FxtrServiceData
from fxtr2.services.subscription_types import Subscriber
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from .client_protocol import CallbackEvent
from .protocols import (
    SESSION_EVENT_INBOX_TYPE_ID,
    SESSION_INBOX_SUBSCRIBER_TYPE_ID,
    WorkstationSessionEventInbox,
)

if TYPE_CHECKING:
    from .state import WorkstationState

_log = logging.getLogger(__name__)

#: Bound on the workstation's best-effort ``unsubscribe()`` dispatch
#: during subscription teardown. A source that doesn't answer within
#: this window is torn down anyway (it discovers the loss via the
#: dispatch error on its next ``on_event``).
#: TODO(daniel): expose fire-and-forget semantics to avoid having to
#: wait for the unsubscribe at all
UNSUBSCRIBE_TIMEOUT_SECONDS = 1.0


@dataclasses.dataclass
class _InboxSubscription:
    """One ``subscribe_to`` subscription's shared state — held on the
    inbox impl (for the close-path wakeups and the per-source dedup)
    and by the parker task that owns the subscription's scope."""

    source_key: ServiceKey
    """:class:`ServiceKey` of the event source. Retained for logging /
    debugging; the single-subscription check no longer compares keys."""

    unsubscribe_requested: anyio.Event
    """Fired (sync, idempotent) by :meth:`WorkstationInboxImpl.mark_closed`
    — the close-callback commit and the session-close sweep — and by
    :func:`_on_inbox_subscription_broken` on an ungraceful source break.
    The parker task wakes, (unless broken) unsubscribes from the source,
    and tears the subscription down."""

    done: bool = False
    """Set by the parker task on exit. A ``done`` entry no longer counts
    as a live subscription."""

    broken: bool = False
    """Set (sync, idempotent) by :func:`_on_inbox_subscription_broken`
    when the source's keep-alive lease breaks. A ``broken`` entry no
    longer counts as a live subscription, and the parker skips its
    best-effort ``unsubscribe()`` on teardown (the source is already
    gone)."""


class WorkstationInboxImpl(TypedServiceImpl[WorkstationSessionEventInbox]):
    """Receive-side impl for a session-owned callback slot.

    Carries enough back-references (``state`` / ``session_id`` /
    ``slot``) for the 3-phase delivery to route events into the right
    session's event log, plus ``impl_scope`` (the scope it's mounted
    on, owned by ``callback_record_worker``) as the parent for
    per-subscription scopes. ``state`` is typed via
    :data:`typing.TYPE_CHECKING` to break the import cycle —
    :mod:`.state` imports this class for its
    :class:`~fxtr_workstation.state.CallbackRecord` bookkeeping, so
    the runtime import only goes one way.

    The ``_closed`` flag is the single source of truth for "is the
    receive side still listening". It's flipped by :meth:`mark_closed`
    (the :class:`~fxtr_workstation.client_protocol.CloseCallbackOp`
    commit and the session-close sweep); until then it stays
    ``False``.
    """

    def __init__(
        self,
        state: WorkstationState,
        session_id: UUID,
        slot: int,
        *,
        impl_scope: FxtrScope,
    ) -> None:
        self._state = state
        self._session_id = session_id
        self._slot = slot
        self._impl_scope = impl_scope
        self._closed: bool = False
        # ``subscribe_to`` subscriptions; mutated by ``subscribe_to``
        # (append) and read by ``mark_closed`` (wakeups) and the
        # per-source dedup scan. Entries stay until the inbox closes
        # (there is no per-source unsubscribe op in v1).
        self._subscriptions: list[_InboxSubscription] = []

    def _target_type_annotation(self) -> Any:
        return WorkstationSessionEventInbox

    def _service_type_id(self) -> FxtrID:
        return SESSION_EVENT_INBOX_TYPE_ID

    def mark_closed(self) -> None:
        """Close the receive side: flip ``_closed`` and wake every
        subscription's parker task for unsubscribe + teardown.

        Sync and idempotent — called from the sync commit phase
        (:class:`~fxtr_workstation.client_protocol.CloseCallbackOp`)
        and the session-close sweep; the parker tasks do the async
        unsubscribe work.
        """
        self._closed = True
        for entry in self._subscriptions:
            entry.unsubscribe_requested.set()

    async def _deliver(self, payload: FxtrServiceData) -> bool:
        """Run the shared 3-phase delivery into the owning session's
        event log; returns ``False`` if the session is gone.

        Captures the active dispatch scope (the per-call ``call_scope``
        opened by the RPC layer) as the ``source_scope`` for the
        3-phase delivery — entity / service leaves in ``payload`` are
        looked up against the caller's scope chain through it. The
        3-phase machinery is shared with invoke-result delivery (see
        :func:`~fxtr_workstation.state._deliver_result_as_event`); the
        only callback-specific knob is the :class:`CallbackEvent`-
        building lambda.
        """
        # Late import — :mod:`.state` imports this module for its
        # ``CallbackRecord`` bookkeeping, so the runtime import only
        # goes one way at module load. Pulling
        # ``_deliver_result_as_event`` in here breaks the cycle.
        from .state import _deliver_result_as_event

        slot = self._slot
        return await _deliver_result_as_event(
            self._state,
            self._session_id,
            payload,
            get_active_scope(),
            build_result_event=lambda seq, escaped: CallbackEvent(
                seq=seq, slot=slot, payload=escaped
            ),
        )

    @service_method
    async def subscribe_to(
        self, event_source: ServiceRef[Any], method: str = "subscribe", /, **kwargs: FxtrServiceData
    ) -> None:
        """Subscribe this inbox to ``event_source`` until the inbox
        closes.

        ``event_source`` / ``method`` are positional-only; ``method``
        (default ``"subscribe"``) names the source's subscription method;
        it is dispatched against the source as an untyped service (see
        :func:`_inbox_subscription_parker`), so the source need not
        statically be an ``EventSource`` — any service exposing
        ``method(subscriber) -> UnsubscribeHandle`` works. ``**kwargs``
        are forwarded after the subscriber ref —
        ``method(subscriber, **kwargs)`` — for parameterized subscription
        surfaces (whose extra parameters are keyword-only by convention);
        they are ordinary wire data, decoded by the source's own
        dispatch.

        **Exactly once.** An inbox may have at most one *live*
        subscription at a time; any ``subscribe_to`` while one is live
        raises :class:`RuntimeError` — including a repeat for the same
        source (subscribing to the same thing twice is not a real use
        case, so we error rather than silently no-op). A "live"
        subscription is one that is neither torn down (``done``) nor
        mid-teardown after a break (``broken``); to subscribe again the
        client closes / forgets the inbox slot, or waits for the current
        subscription to break.

        On a fresh subscribe, spawns a per-subscription parker task on
        ``state.background_tg`` via ``await background_tg.start(...)`` —
        this dispatch returns only once the subscription is fully
        established (scope open, source bridged, internal subscriber
        mounted, ``source.subscribe`` completed). Failures before that
        point propagate to the caller (surfacing as an ``invoke_error``
        on the invoking session's slot) and the subscription is not
        retained. On success, a :class:`CallbackSubscriptionEvent`
        (``connected=True``) is appended to the owning session's event
        log so clients can track the inbox's connection state.
        """
        # Late import — :mod:`.state` imports this module for its
        # ``CallbackRecord`` bookkeeping, so the runtime import only
        # goes one way at module load.
        from .state import _emit_callback_subscription_event

        if self._closed:
            raise RuntimeError(
                "subscribe_to on a closed inbox: the callback slot was already "
                "closed (close_callback op or session close)."
            )
        if any(not e.done and not e.broken for e in self._subscriptions):
            raise RuntimeError(
                "subscribe_to on an inbox that already has a live subscription: "
                "an inbox may be subscribed to exactly one source at a time. "
                "Close/forget the callback slot (or wait for the current "
                "subscription to break) before subscribing again."
            )
        # Prune retired entries so the list can't grow unbounded across
        # break/resubscribe cycles.
        self._subscriptions = [e for e in self._subscriptions if not e.done]
        key = ServiceKey(handle=event_source.handle, type_id=event_source.service_type_id)
        entry = _InboxSubscription(source_key=key, unsubscribe_requested=anyio.Event())
        self._subscriptions.append(entry)
        try:
            await self._state.background_tg.start(
                _inbox_subscription_parker,
                self,
                entry,
                event_source,
                get_active_scope(),
                method,
                kwargs,
            )
        except BaseException:
            # Parker failed before signaling ``started`` — typically
            # the source's ``subscribe`` raising. Un-register so we
            # don't leave a stale entry blocking a retry.
            try:
                self._subscriptions.remove(entry)
            except ValueError:
                pass
            raise
        # Subscription fully established — announce the connected
        # transition to the owning session.
        _emit_callback_subscription_event(self._state, self._session_id, self._slot, connected=True)

    @service_method
    async def notify(self, payload: FxtrServiceData) -> None:
        """Push ``payload`` into the originating session's event log as
        a :class:`~fxtr_workstation.client_protocol.CallbackEvent`.

        Silent no-op when the receive side is closed (close-callback or
        session-close already ran) or when the session goes away
        mid-delivery — direct push is fire-and-forget; callers that
        care probe :meth:`listening` first.

        Raises:
            Any exception from the helper's phase-2 bridge-up or
            phase-3 codec failure. These propagate to the RPC peer as
            method-call errors; the session's event log only sees
            successful deliveries. Pre-decref of any in-flight
            contributions is handled by the helper itself.
        """
        if self._closed:
            return
        await self._deliver(payload)

    @service_method
    async def listening(self) -> bool:
        return not self._closed


class _InboxSubscriberImpl(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """The internal :class:`Subscriber` an inbox mounts per
    ``subscribe_to`` subscription — the thing actually handed to
    ``EventSource.subscribe``.

    ``on_event`` runs the same 3-phase delivery as direct-push
    ``notify``. When the inbox is closed it silently drops — but only
    during the brief window between the ``_closed`` flip and the parker
    task's forceful teardown; after teardown, the source's dispatches
    error out and its crash backstop reaps the subscription (teardown
    *is* the durable "you're done" signal).
    """

    def __init__(self, inbox: WorkstationInboxImpl) -> None:
        self._inbox = inbox

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return SESSION_INBOX_SUBSCRIBER_TYPE_ID

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        if self._inbox._closed:
            return
        await self._inbox._deliver(payload)


def _on_inbox_subscription_broken(
    inbox: WorkstationInboxImpl,
    entry: _InboxSubscription,
    reason: str,
) -> None:
    """Broken-callback for an inbox subscription's keep-alive lease.

    Fires synchronously when the source's grant is revoked / its RPC
    connection drops (possibly from the RPC reader task). Marks the
    entry ``broken`` (so the parker skips the doomed unsubscribe and the
    single-subscription check stops counting it as live), emits a
    ``connected=False`` :class:`CallbackSubscriptionEvent` carrying the
    break ``reason`` to the owning session, and wakes the parker to tear
    the subscription down. Idempotent — re-breaks (or a break racing the
    parker's exit) are no-ops. Does only sync registry work +
    ``send_nowait``, consistent with the file's GIL-atomicity
    discipline.
    """
    if entry.broken or entry.done:
        return
    # Late import — breaks the module-load cycle with :mod:`.state`.
    from .state import _emit_callback_subscription_event

    _log.info(
        "inbox subscription (session %s slot %d) broke: %s",
        inbox._session_id,
        inbox._slot,
        reason,
    )
    entry.broken = True
    _emit_callback_subscription_event(
        inbox._state, inbox._session_id, inbox._slot, connected=False, reason=reason
    )
    entry.unsubscribe_requested.set()


async def _inbox_subscription_parker(
    inbox: WorkstationInboxImpl,
    entry: _InboxSubscription,
    event_source: ServiceRef[Any],
    source_scope: FxtrScope,
    method: str,
    kwargs: dict[str, FxtrServiceData],
    *,
    task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Per-subscription parker task. Owns the subscription end-to-end.

    Lifecycle (mirrors ``callback_record_worker``'s shape):

    1. Open an isolated mountable subscription scope
       (``include=[impl_scope]``).
    2. Bridge ``event_source`` in from the ``subscribe_to`` dispatch's
       call scope; mount an internal :class:`_InboxSubscriberImpl`;
       resolve the source as an **untyped** service and dispatch
       ``method`` (default ``"subscribe"``) with the subscriber ref +
       any forwarded ``kwargs`` — so the source need not statically be
       an ``EventSource``, only expose
       ``method(subscriber, **kwargs) -> UnsubscribeHandle``. The returned
       :class:`~fxtr2.services.subscription_types.UnsubscribeHandle`
       ref installs into ``sub_scope`` — the home scope of the call —
       via the standard return path.
    3. Signal ``task_status.started()`` — ``subscribe_to``'s dispatch
       returns only once the subscription is fully established;
       failures before this point propagate to the caller.
    4. Park on the entry's ``unsubscribe_requested`` event.
    5. On wake (or cancel): best-effort ``handle.unsubscribe()``
       (shielded + bounded by :data:`UNSUBSCRIBE_TIMEOUT_SECONDS`, so
       it also runs on task-group cancel), log on timeout / error,
       then tear the subscription scope down **forcefully** (see the
       module docstring for why forceful).

    Owning the scope entirely inside one task sidesteps any
    open-here/close-elsewhere scope questions.
    """
    try:
        async with FxtrScope(
            isolated=True,
            include=[inbox._impl_scope],
            name=f"inbox_subscription/{inbox._session_id}/{inbox._slot}",
        ) as sub_scope:
            try:
                await sub_scope.bridge_service_from_scope(
                    event_source, source_scope, keepalive_via="independent_lease"
                )
                # Observe the gating grant: register a broken-callback on
                # the bridge's standing keep-alive lease so we learn when
                # the source's grant is revoked / its RPC connection
                # drops. Mirrors ``service_record_worker`` in
                # :mod:`.state`. ``None`` only for non-independent bridge
                # shapes, which the ``independent_lease`` keepalive above
                # does not produce; log if that ever changes.
                keepalive = sub_scope._peek_independent_keepalive_lease(event_source.handle)
                if keepalive is not None:
                    keepalive.register_broken_callback(
                        lambda reason: _on_inbox_subscription_broken(inbox, entry, reason)
                    )
                else:
                    _log.warning(
                        "inbox subscription (session %s slot %d) bridged without an "
                        "independent keep-alive lease; break notifications disabled",
                        inbox._session_id,
                        inbox._slot,
                    )
                subscriber_ref = await sub_scope.mount(_InboxSubscriberImpl(inbox))
                # Dispatch ``method`` against the source as an untyped
                # service (mirrors ``state.py``'s invoke dispatch), so a
                # source whose subscription method isn't literally
                # ``EventSource.subscribe`` still works. With the default
                # ``method`` this is exactly the ``EventSource.subscribe``
                # contract.
                source_proxy = await event_source.replace_service_type(UnknownService).resolve(
                    sub_scope
                )
                handle_ref = await getattr(source_proxy, method)(subscriber_ref, **kwargs)
                handle_proxy = await handle_ref.resolve(sub_scope)
                task_status.started()
                try:
                    await entry.unsubscribe_requested.wait()
                finally:
                    if entry.broken:
                        # The source's grant already broke — its lease
                        # fired our broken-callback. A best-effort
                        # ``unsubscribe()`` would only burn
                        # ``UNSUBSCRIBE_TIMEOUT_SECONDS`` dispatching
                        # against a dead source, so skip straight to the
                        # forceful teardown below.
                        _log.info(
                            "inbox subscription (session %s slot %d) broke; "
                            "skipping unsubscribe and tearing down",
                            inbox._session_id,
                            inbox._slot,
                        )
                    else:
                        # Best-effort unsubscribe — the release signal
                        # for the source. Shielded so it also runs when
                        # the park was interrupted by task-group cancel
                        # (server shutdown); bounded so a stuck source
                        # can't block teardown.
                        with anyio.move_on_after(
                            UNSUBSCRIBE_TIMEOUT_SECONDS, shield=True
                        ) as cancel_scope:
                            try:
                                await handle_proxy.unsubscribe()
                            except Exception:
                                _log.warning(
                                    "inbox subscription (session %s slot %d): "
                                    "unsubscribe failed; tearing down anyway",
                                    inbox._session_id,
                                    inbox._slot,
                                    exc_info=True,
                                )
                        if cancel_scope.cancelled_caught:
                            _log.warning(
                                "inbox subscription (session %s slot %d): "
                                "unsubscribe timed out after %.1fs; tearing down "
                                "anyway",
                                inbox._session_id,
                                inbox._slot,
                                UNSUBSCRIBE_TIMEOUT_SECONDS,
                            )
            finally:
                # Forceful teardown on every exit path — the durable
                # "you're done" signal + the cross-pin breaker (see
                # module docstring).
                sub_scope.force_close()
    finally:
        entry.done = True
