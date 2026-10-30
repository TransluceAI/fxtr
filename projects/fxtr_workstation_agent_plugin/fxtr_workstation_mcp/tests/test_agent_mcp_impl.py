"""Unit tests for :mod:`fxtr_workstation_mcp.agent_mcp_impl`.

Verifies the impl + the hub-backed subscription model in isolation: no
real workstation, no /rpc dispatch. The impl methods are called
directly inside an active impl scope; the subscriber / inbox-callback
refs are satisfied by mounting mock impls on a companion scope that's
bridge-source-compatible with the impl_scope.

Covered scenarios:

- ``get_state`` returns the constructor's ``initial_head`` (including
  ``None``) until ``update_head`` replaces it.
- ``update_head`` publishes to subscribers; a subscriber's
  ``on_event`` sees the new head.
- **Skip-ahead semantics**: two rapid-fire ``update_head`` calls
  while a subscriber is mid-``on_event`` collapse — the subscriber's
  next ``on_event`` sees the *latest* head, intermediate heads are
  skipped (coalesce-mode hub).
- **Ordering**: a subscriber never observes heads out of publish
  order.
- Invoking the returned :class:`UnsubscribeHandle` ends the
  subscription; a later ``update_head`` doesn't fire, and the same
  subscriber can re-subscribe afterwards.
- A subscriber whose ``on_event`` raises is dropped (crash backstop)
  without affecting siblings.
- Duplicate ``subscribe`` for the same subscriber creates an
  independent subscription (no dedup — duplicates just duplicate
  deliveries).
- **deliver-current-as-first-event**: the constructor's
  ``initial_head`` / any pre-subscribe ``update_head`` is delivered
  as the subscription's first event; a fresh session (no head ever)
  delivers nothing until the first extension. Under a concurrent
  ``update_head``, the new head is observed exactly once (the
  no-dup-no-miss window inside the hub).
- ``send`` invokes ``notify`` on the configured inbox-callback ref.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import anyio.abc
import pytest
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_workstation.protocols import (
    SESSION_EVENT_INBOX_TYPE_ID,
    WorkstationSessionEventInbox,
)

from fxtr_workstation_mcp.agent_log_types import WsAgentLogEventChain
from fxtr_workstation_mcp.agent_mcp_impl import AgentMcpRunningSessionImpl

pytestmark = pytest.mark.anyio


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.005)


# --- Fixtures + mocks -------------------------------------------------


@pytest.fixture
async def scopes() -> AsyncIterator[tuple[FxtrScope, FxtrScope]]:
    """Yield ``(impl_scope, listener_scope)`` — distinct mountable
    scopes that share no parent so we can bridge between them.

    ``impl_scope`` holds the :class:`AgentMcpRunningSessionImpl` and
    bridged subscriber refs. ``listener_scope`` holds the test's mock
    mounts. The active scope at yield time is ``impl_scope`` so the
    test's ``ref.load()`` / mount construction defaults route there.

    Order matters: bridging from ``listener_scope`` into
    ``impl_scope`` pins ``listener_scope`` until ``impl_scope``
    drains. ``async with (A, B)`` exits B before A on the way out;
    so ``impl_scope`` must be the *inner* (B) one — it drains first,
    releasing the bridge, then ``listener_scope`` (A) can drain.
    """
    async with (
        FxtrScope(
            name="agent_mcp_impl_test_listeners",
            isolated=True,
        ) as listener_scope,
        FxtrScope(
            name="agent_mcp_impl_test_impl",
            isolated=True,
        ) as impl_scope,
    ):
        with impl_scope.as_active_scope():
            yield impl_scope, listener_scope


class _MockInboxImpl(TypedServiceImpl[WorkstationSessionEventInbox]):
    """Minimal inbox mock for the ``send`` path — records ``notify``
    payloads. (``subscribe_to`` is never dispatched in these tests.)"""

    def __init__(self) -> None:
        self.notify_payloads: list[Any] = []

    def _target_type_annotation(self) -> Any:
        return WorkstationSessionEventInbox

    def _service_type_id(self) -> FxtrID:
        return SESSION_EVENT_INBOX_TYPE_ID

    @service_method
    async def notify(self, payload: FxtrServiceData) -> None:
        self.notify_payloads.append(payload)

    @service_method
    async def listening(self) -> bool:
        return True


class _RecordingSubscriber(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """``Subscriber`` impl that records every ``on_event`` call.

    Configurable behavior:

    - ``raise_exc``: optional :class:`Exception` to raise from
      ``on_event`` (after recording). Tests use this to verify the
      hub drops a subscription whose subscriber raised.
    - ``before_return``: optional async hook awaited before
      ``on_event`` returns. Tests use this to wedge a deliberate
      yield-point between firings (so rapid-fire updates can
      collapse).
    """

    def __init__(
        self,
        *,
        raise_exc: Exception | None = None,
        before_return: Any = None,
    ) -> None:
        self._raise_exc = raise_exc
        self._before_return = before_return
        self.payloads: list[Any] = []

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("test-agent-mcp-subscriber"))

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        self.payloads.append(payload)
        if self._before_return is not None:
            await self._before_return()
        if self._raise_exc is not None:
            raise self._raise_exc


def _alloc_id(scope: FxtrScope, name: str) -> FxtrID:
    fid = FxtrID.builtin(f"test_agent_mcp_impl_{name}_{uuid.uuid4().hex}")
    scope.track_builtin(fid)
    return fid


def _mk_chain_head(scope: FxtrScope, name: str) -> EntityRef[WsAgentLogEventChain]:
    """Synthetic chain-head EntityRef for ``update_head`` testing.

    The EntityRef points at a builtin-tracked FxtrID. We don't need
    the entity to actually load — the impl + subscribers only pass
    the ref around, never .load() it. Tests assert on ref identity
    (entity_id equality) only.
    """
    return EntityRef(entity_id=_alloc_id(scope, name), target_type=WsAgentLogEventChain)


async def _mount_subscriber(
    scope: FxtrScope, **kwargs: Any
) -> tuple[_RecordingSubscriber, ServiceRef[Subscriber[FxtrServiceData]]]:
    """Mount a recording subscriber on ``scope``; return (impl, ref)."""
    subscriber = _RecordingSubscriber(**kwargs)
    ref = await scope.mount(subscriber)
    return subscriber, ref


# --- Helpers ----------------------------------------------------------


async def _make_impl(
    impl_scope: FxtrScope,
    *,
    listener_scope: FxtrScope,
    impl_tg: anyio.abc.TaskGroup,
    initial_head: EntityRef[WsAgentLogEventChain] | None = None,
) -> tuple[AgentMcpRunningSessionImpl, _MockInboxImpl]:
    """Build an impl with an inbox-callback mock mounted on
    ``listener_scope`` and bridged into ``impl_scope`` (mirrors the
    bootstrap flow). The workstation_ref field is satisfied by
    reusing the inbox ref — the impl methods don't actually dispatch
    through it, so any :class:`ServiceRef` typed as
    :class:`WorkstationServiceProto` will do for unit-test purposes."""
    inbox_impl = _MockInboxImpl()
    inbox_ref = inbox_impl_ref = await listener_scope.mount(inbox_impl)
    del inbox_impl_ref
    # Bridge the inbox ref into the impl scope, same as the real
    # bootstrap would do.
    await impl_scope.bridge_service_from_scope(
        inbox_ref, listener_scope, keepalive_via="independent_lease"
    )
    impl = AgentMcpRunningSessionImpl(
        scope=impl_scope,
        workstation_ref=inbox_ref,  # type: ignore[arg-type]
        inbox_callback_ref=inbox_ref,
        initial_head=initial_head,
        impl_tg=impl_tg,
    )
    return impl, inbox_impl


async def _call_subscribe(
    impl: AgentMcpRunningSessionImpl,
    subscriber_ref: ServiceRef[Subscriber[FxtrServiceData]],
    listener_scope: FxtrScope,
) -> ServiceRef[UnsubscribeHandle]:
    """Test-side helper: invoke ``impl.subscribe(subscriber_ref)``
    with ``listener_scope`` set as the active scope; return the
    unsubscribe-handle ref.

    In real production, the typed-service framework dispatches each
    method call inside a fresh per-call dispatch scope that decodes
    args. ``subscribe`` reads ``get_active_scope()`` as its bridge
    source. Our unit tests bypass the framework, so we activate
    ``listener_scope`` here as the stand-in — the subscriber ref is
    mounted there, and bridging from it into the hub's subscription
    scope works the same way as in production.
    """
    with listener_scope.as_active_scope():
        return await impl.subscribe(subscriber_ref)


def _live_count(impl: AgentMcpRunningSessionImpl) -> int:
    return impl._hub.live_subscription_count  # type: ignore[reportPrivateUsage]


# --- get_state --------------------------------------------------------


async def test_get_state_initial_none(scopes: tuple[FxtrScope, FxtrScope]) -> None:
    """``initial_head=None`` → ``get_state`` returns ``None`` until
    ``update_head`` writes."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        assert await impl.get_state() is None
        tg.cancel_scope.cancel()


async def test_get_state_initial_head(scopes: tuple[FxtrScope, FxtrScope]) -> None:
    """Constructor's ``initial_head`` is returned by ``get_state``."""
    impl_scope, listener_scope = scopes
    head = _mk_chain_head(impl_scope, "initial")
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(
            impl_scope, listener_scope=listener_scope, impl_tg=tg, initial_head=head
        )
        observed = await impl.get_state()
        assert observed is not None
        assert observed.entity_id == head.entity_id
        tg.cancel_scope.cancel()


async def test_get_state_reflects_update(scopes: tuple[FxtrScope, FxtrScope]) -> None:
    """``update_head`` is observed by subsequent ``get_state`` calls."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        new_head = _mk_chain_head(impl_scope, "updated")
        impl.update_head(new_head)
        observed = await impl.get_state()
        assert observed is not None
        assert observed.entity_id == new_head.entity_id
        tg.cancel_scope.cancel()


# --- subscribe + update_head -------------------------------------------


async def test_subscriber_fires_on_update(scopes: tuple[FxtrScope, FxtrScope]) -> None:
    """A subscriber's ``on_event`` fires once with the new head."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        subscriber, sub_ref = await _mount_subscriber(listener_scope)

        handle_ref = await _call_subscribe(impl, sub_ref, listener_scope)
        assert handle_ref.target_type is UnsubscribeHandle

        head = _mk_chain_head(impl_scope, "h1")
        impl.update_head(head)

        await _wait_until(lambda: bool(subscriber.payloads))
        assert len(subscriber.payloads) == 1
        # The typed-service framework wire-encodes the EntityRef
        # argument as a bare ``FxtrID`` per its ``FxtrServiceData``
        # signature, so the subscriber receives the entity_id.
        payload = subscriber.payloads[0]
        assert isinstance(payload, FxtrID)
        assert payload == head.entity_id

        tg.cancel_scope.cancel()


async def test_subscriber_skip_ahead_under_rapid_updates(
    scopes: tuple[FxtrScope, FxtrScope],
) -> None:
    """A subscriber whose ``on_event`` is slow sees the *latest* head
    on its next iteration; intermediate updates collapse."""
    impl_scope, listener_scope = scopes
    # Hook to make on_event slow enough that we can issue multiple
    # update_head calls before the first delivery completes.
    slow_event = anyio.Event()

    async def _slow_before_return() -> None:
        await slow_event.wait()

    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        subscriber, sub_ref = await _mount_subscriber(
            listener_scope, before_return=_slow_before_return
        )
        await _call_subscribe(impl, sub_ref, listener_scope)

        h1 = _mk_chain_head(impl_scope, "h1")
        h2 = _mk_chain_head(impl_scope, "h2")
        h3 = _mk_chain_head(impl_scope, "h3")

        # Fire the first update — the worker wakes and starts the
        # delivery (which now blocks on slow_event). Wait until it's
        # running.
        impl.update_head(h1)
        await _wait_until(lambda: bool(subscriber.payloads))
        # The first delivery is in flight; issue two more updates
        # while it's blocked.
        impl.update_head(h2)
        impl.update_head(h3)
        # Now unblock the first delivery.
        slow_event.set()

        # The worker should fire one more delivery with the latest
        # (h3), skipping h2.
        await _wait_until(lambda: len(subscriber.payloads) >= 2)

        # Give the loop a moment to confirm it doesn't fire a third
        # time. (slow_event stays set, so the second delivery isn't
        # blocked.)
        await anyio.sleep(0.05)
        assert len(subscriber.payloads) == 2
        assert subscriber.payloads[0] == h1.entity_id
        assert subscriber.payloads[1] == h3.entity_id

        tg.cancel_scope.cancel()


async def test_subscriber_ordering_preserved(scopes: tuple[FxtrScope, FxtrScope]) -> None:
    """Sequential updates with no overlap fire in order (no inversion)."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        subscriber, sub_ref = await _mount_subscriber(listener_scope)
        await _call_subscribe(impl, sub_ref, listener_scope)

        heads = [_mk_chain_head(impl_scope, f"h{i}") for i in range(5)]
        for i, h in enumerate(heads):
            impl.update_head(h)
            # Drain each update individually before the next one
            # lands, so nothing coalesces.
            await _wait_until(lambda: len(subscriber.payloads) >= i + 1)

        assert list(subscriber.payloads) == [h.entity_id for h in heads]
        tg.cancel_scope.cancel()


# --- unsubscribe + crash backstop ---------------------------------------


async def test_unsubscribe_stops_deliveries_and_resubscribe_works(
    scopes: tuple[FxtrScope, FxtrScope],
) -> None:
    """Invoking the returned handle ends the subscription (later
    updates don't fire), and the same subscriber can re-subscribe
    afterwards — proves the per-subscription scope drained and the
    bridge released."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        subscriber, sub_ref = await _mount_subscriber(listener_scope)
        handle_ref = await _call_subscribe(impl, sub_ref, listener_scope)

        impl.update_head(_mk_chain_head(impl_scope, "h1"))
        await _wait_until(lambda: len(subscriber.payloads) == 1)

        handle = await handle_ref.resolve(listener_scope)
        await handle.unsubscribe()
        await _wait_until(lambda: _live_count(impl) == 0)

        impl.update_head(_mk_chain_head(impl_scope, "h2"))
        await anyio.sleep(0.05)
        assert len(subscriber.payloads) == 1

        # Re-subscribe the same subscriber ref; the new subscription
        # fires. (subscribe seeds the impl's retained head — h2 — as
        # the first event.)
        await _call_subscribe(impl, sub_ref, listener_scope)
        await _wait_until(lambda: len(subscriber.payloads) == 2)
        h3 = _mk_chain_head(impl_scope, "h3")
        impl.update_head(h3)
        await _wait_until(lambda: len(subscriber.payloads) == 3)
        assert subscriber.payloads[2] == h3.entity_id

        tg.cancel_scope.cancel()


async def test_subscriber_dropped_on_on_event_exception(
    scopes: tuple[FxtrScope, FxtrScope],
) -> None:
    """A subscriber whose ``on_event`` raises is dropped cleanly
    (crash backstop) without disturbing a sibling."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        bad, bad_ref = await _mount_subscriber(listener_scope, raise_exc=RuntimeError("oops"))
        good, good_ref = await _mount_subscriber(listener_scope)
        await _call_subscribe(impl, bad_ref, listener_scope)
        await _call_subscribe(impl, good_ref, listener_scope)
        assert _live_count(impl) == 2

        impl.update_head(_mk_chain_head(impl_scope, "h1"))
        await _wait_until(lambda: bool(bad.payloads) and bool(good.payloads))
        # The raiser's subscription tears down...
        await _wait_until(lambda: _live_count(impl) == 1)
        # ...and the sibling keeps receiving.
        impl.update_head(_mk_chain_head(impl_scope, "h2"))
        await _wait_until(lambda: len(good.payloads) == 2)
        assert len(bad.payloads) == 1
        tg.cancel_scope.cancel()


async def test_duplicate_subscribe_creates_independent_subscription(
    scopes: tuple[FxtrScope, FxtrScope],
) -> None:
    """Subscriptions are not deduplicated: subscribing the same
    subscriber twice doubles its deliveries (each subscription has
    its own handle)."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        subscriber, sub_ref = await _mount_subscriber(listener_scope)

        await _call_subscribe(impl, sub_ref, listener_scope)
        await _call_subscribe(impl, sub_ref, listener_scope)
        assert _live_count(impl) == 2

        head = _mk_chain_head(impl_scope, "h1")
        impl.update_head(head)
        await _wait_until(lambda: len(subscriber.payloads) == 2)
        await anyio.sleep(0.05)
        assert subscriber.payloads == [head.entity_id, head.entity_id]
        tg.cancel_scope.cancel()


async def test_multiple_distinct_subscribers_each_fire(
    scopes: tuple[FxtrScope, FxtrScope],
) -> None:
    """Two distinct subscribers both receive each update."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        sub_a, ref_a = await _mount_subscriber(listener_scope)
        sub_b, ref_b = await _mount_subscriber(listener_scope)
        await _call_subscribe(impl, ref_a, listener_scope)
        await _call_subscribe(impl, ref_b, listener_scope)

        head = _mk_chain_head(impl_scope, "h1")
        impl.update_head(head)

        await _wait_until(lambda: bool(sub_a.payloads) and bool(sub_b.payloads))
        assert sub_a.payloads[0] == head.entity_id
        assert sub_b.payloads[0] == head.entity_id
        tg.cancel_scope.cancel()


# --- deliver-current-as-first-event -------------------------------------


async def test_fresh_session_delivers_nothing_until_first_update(
    scopes: tuple[FxtrScope, FxtrScope],
) -> None:
    """``initial_head=None`` + no ``update_head`` → a new subscriber
    receives nothing; the first extension then arrives normally."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        subscriber, sub_ref = await _mount_subscriber(listener_scope)
        await _call_subscribe(impl, sub_ref, listener_scope)

        await anyio.sleep(0.1)
        assert subscriber.payloads == []

        head = _mk_chain_head(impl_scope, "first")
        impl.update_head(head)
        await _wait_until(lambda: subscriber.payloads == [head.entity_id])
        tg.cancel_scope.cancel()


async def test_initial_head_delivered_as_first_event(
    scopes: tuple[FxtrScope, FxtrScope],
) -> None:
    """Constructor's ``initial_head`` arrives as the subscription's
    first event (replacing the old ``watch_state`` snapshot-return)."""
    impl_scope, listener_scope = scopes
    head = _mk_chain_head(impl_scope, "initial")
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(
            impl_scope, listener_scope=listener_scope, impl_tg=tg, initial_head=head
        )
        subscriber, sub_ref = await _mount_subscriber(listener_scope)
        await _call_subscribe(impl, sub_ref, listener_scope)
        await _wait_until(lambda: subscriber.payloads == [head.entity_id])
        tg.cancel_scope.cancel()


async def test_pre_subscribe_update_delivered_as_first_event(
    scopes: tuple[FxtrScope, FxtrScope],
) -> None:
    """An ``update_head`` that lands before the subscribe is delivered
    as the first event (the hub retains the latest published value).
    A subsequent update follows normally — no duplicates."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        head_pre = _mk_chain_head(impl_scope, "pre")
        impl.update_head(head_pre)

        subscriber, sub_ref = await _mount_subscriber(listener_scope)
        await _call_subscribe(impl, sub_ref, listener_scope)
        await _wait_until(lambda: subscriber.payloads == [head_pre.entity_id])

        head_post = _mk_chain_head(impl_scope, "post")
        impl.update_head(head_post)
        await _wait_until(lambda: subscriber.payloads == [head_pre.entity_id, head_post.entity_id])
        tg.cancel_scope.cancel()


async def test_no_dup_no_miss_under_concurrent_update(
    scopes: tuple[FxtrScope, FxtrScope],
) -> None:
    """Concurrent ``update_head`` + ``subscribe``: across whatever
    interleaving anyio's scheduler picks, the new head is observed
    **exactly once** and nothing is duplicated.

    With the head riding the stream (the subscribe-time seed) instead
    of a return value, the reachable outcomes are:

    - Updater ran before the hub's seed window → the seed *is*
      ``head_b``; one delivery of ``head_b``.
    - Updater ran after the seed but before the first delivery →
      coalesce-mode supersede: the seed (``head_a``) is replaced and
      only ``head_b`` is delivered.
    - Updater ran after the first delivery → ``head_a`` then
      ``head_b``.

    In every case ``head_b`` appears exactly once and no value
    repeats. (``head_a`` may legitimately be absent — superseded.)
    """
    impl_scope, listener_scope = scopes
    head_a = _mk_chain_head(impl_scope, "before")
    head_b = _mk_chain_head(impl_scope, "race")
    async with anyio.create_task_group() as tg:
        impl, _ = await _make_impl(
            impl_scope, listener_scope=listener_scope, impl_tg=tg, initial_head=head_a
        )
        subscriber, sub_ref = await _mount_subscriber(listener_scope)

        async def _updater() -> None:
            impl.update_head(head_b)

        tg.start_soon(_updater)
        await _call_subscribe(impl, sub_ref, listener_scope)

        # Wait until head_b lands, then a beat for any (incorrect)
        # extra deliveries.
        await _wait_until(lambda: head_b.entity_id in subscriber.payloads)
        await anyio.sleep(0.1)

        observed = list(subscriber.payloads)
        assert observed.count(head_b.entity_id) == 1, observed
        assert len(observed) == len(set(observed)), f"duplicate in {observed}"

        tg.cancel_scope.cancel()


# --- send -------------------------------------------------------------


async def test_send_invokes_inbox_notify(scopes: tuple[FxtrScope, FxtrScope]) -> None:
    """``send(payload)`` ends up dispatching ``notify`` on the
    inbox-callback ref the impl was constructed with."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, inbox = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        await impl.send({"hello": "world"})
        assert inbox.notify_payloads == [{"hello": "world"}]
        tg.cancel_scope.cancel()


async def test_send_dispatches_multiple_payloads_in_order(
    scopes: tuple[FxtrScope, FxtrScope]
) -> None:
    """Multiple ``send`` calls land on the inbox listener in order."""
    impl_scope, listener_scope = scopes
    async with anyio.create_task_group() as tg:
        impl, inbox = await _make_impl(impl_scope, listener_scope=listener_scope, impl_tg=tg)
        await impl.send("a")
        await impl.send("b")
        await impl.send({"final": True})
        assert inbox.notify_payloads == ["a", "b", {"final": True}]
        tg.cancel_scope.cancel()
