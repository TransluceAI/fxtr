"""Tests for the subscription primitives + ``SubscriberHub``.

Covers the ``Subscriber`` / ``UnsubscribeHandle`` / ``EventSource``
protocol round-trip over RPC-ish sibling scopes (a hub-backed
``EventSource`` impl mounted in one scope, subscribers mounted in
another), plus the redesigned hub's behaviors: event-borne residency
(origin leases, zero-copy delivery from drained origins, prompt lease
release), seeded subscriptions, handle-drop teardown, the flushing
``aclose``, the non-pinning subscription topology (the headline
property), delivery modes, the crash backstop, and cancel-driven
teardown.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    EntityRef,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.subscription_types import EventSource, Subscriber, UnsubscribeHandle
from fxtr2.services.subscription_util import DeliveryMode, SubscriberHub
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

pytestmark = pytest.mark.anyio


_SUB_TEST_NS = uuid.UUID("9c0a8a64-2b07-44f5-b0d7-66dfc4f7a35d")


@dataclasses.dataclass(frozen=True)
class _Payload(DataclassEntity, fxtr_type=system_entities.define_type(_SUB_TEST_NS, "payload")):
    text: str


# ---------------------------------------------------------------------------
# Test impls
# ---------------------------------------------------------------------------


class _HubSourceImpl(TypedServiceImpl[EventSource[FxtrServiceData]]):
    """``EventSource`` impl whose ``subscribe`` delegates to a hub —
    the intended production shape. ``seed_provider`` (when set) mimics
    the snapshot-seeding source pattern: it returns ``(seed values,
    seed origin scope)`` computed at subscribe time."""

    def __init__(
        self,
        hub: SubscriberHub[FxtrServiceData],
        *,
        seed_provider: Callable[[], tuple[Sequence[Any], FxtrScope | None]] | None = None,
    ):
        super().__init__()
        self.hub = hub
        self._seed_provider = seed_provider

    def _target_type_annotation(self) -> Any:
        return EventSource[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("test-hub-source"))

    @service_method
    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]]
    ) -> ServiceRef[UnsubscribeHandle]:
        if self._seed_provider is not None:
            seed, seed_origin = self._seed_provider()
            return await self.hub.subscribe(
                subscriber, get_active_scope(), seed=seed, seed_origin=seed_origin
            )
        return await self.hub.subscribe(subscriber, get_active_scope())


class _RecordingSubscriberImpl(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """Subscriber that records payloads. Optional knobs:

    - ``raise_on_event``: every ``on_event`` raises (crash-backstop
      tests).
    - ``release_gate``: when set, each ``on_event`` records its payload
      and then blocks on the semaphore — lets a test hold a delivery
      in flight while publishing more values (skip-ahead tests).
    """

    def __init__(self, *, raise_on_event: bool = False):
        super().__init__()
        self.received: list[Any] = []
        self.release_gate: anyio.Semaphore | None = None
        self._raise_on_event = raise_on_event

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("test-recording-subscriber"))

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        if self._raise_on_event:
            raise RuntimeError("subscriber boom")
        self.received.append(payload)
        if self.release_gate is not None:
            await self.release_gate.acquire()


class _LoadingSubscriberImpl(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """Subscriber that loads its (FxtrID-shaped) payloads inside the
    dispatch — exercises payload-entity reachability through the
    per-delivery scope (the event's origin lease) end-to-end."""

    def __init__(self) -> None:
        super().__init__()
        self.loaded_texts: list[str] = []

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("test-loading-subscriber"))

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        assert isinstance(payload, FxtrID)
        loaded = await EntityRef(payload, _Payload).load()
        self.loaded_texts.append(loaded.text)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Harness:
    source_mount: FxtrScope
    caller_scope: FxtrScope
    hub: SubscriberHub[FxtrServiceData]
    impl: _HubSourceImpl
    proxy: Any  # EventSource[FxtrServiceData] proxy resolved in caller_scope

    async def mount_subscriber(self, impl: TypedServiceImpl[Any]) -> ServiceRef[Any]:
        return await self.caller_scope.mount(impl)


@asynccontextmanager
async def _hub_harness(
    mode: DeliveryMode = "coalesce",
    *,
    seed_provider: Callable[[], tuple[Sequence[Any], FxtrScope | None]] | None = None,
) -> AsyncGenerator[_Harness]:
    """Hub-backed EventSource in one scope, callers in a sibling scope,
    bridged across — the RPC-ish layout.

    Teardown cancels the hub task group first (live subscriptions are
    torn down forcefully), which releases the cross-scope pins so the
    scopes can unwind.
    """
    async with FxtrScope(name="source_mount", include=[GLOBAL_SCOPE], isolated=True) as source:
        async with anyio.create_task_group() as tg:
            async with FxtrScope(
                name="hub_caller", include=[GLOBAL_SCOPE], isolated=True
            ) as caller:
                hub = SubscriberHub[FxtrServiceData](task_group=tg, mode=mode)
                impl = _HubSourceImpl(hub, seed_provider=seed_provider)
                source_ref = await source.mount(impl)
                await caller.bridge_service_from_scope(
                    source_ref, source, keepalive_via="independent_lease"
                )
                proxy = await source_ref.resolve(caller)
                try:
                    yield _Harness(
                        source_mount=source,
                        caller_scope=caller,
                        hub=hub,
                        impl=impl,
                        proxy=proxy,
                    )
                finally:
                    tg.cancel_scope.cancel()


def _lowlevel_origin(name: str) -> FxtrScope:
    """A lifespanless scope usable as a publish origin: entities can be
    stored in it, and ``discard_base_authorization`` puts it into the
    draining state (kept open only by captured event leases)."""
    return FxtrScope(name=name, include=[GLOBAL_SCOPE], isolated=True).lowlevel_start_lifespanless(
        parent_leases={GLOBAL_SCOPE: GLOBAL_SCOPE.clone_authorization()}
    )


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)


async def _settle() -> None:
    """Give in-flight deliveries a moment to (not) land."""
    await anyio.sleep(0.1)


# ---------------------------------------------------------------------------
# Subscribe + delivery + unsubscribe round trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["coalesce", "queue"])
async def test_subscribe_delivers_events(mode: DeliveryMode) -> None:
    async with _hub_harness(mode) as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        handle_ref = await h.proxy.subscribe(sub_ref)
        assert isinstance(handle_ref, ServiceRef)
        assert handle_ref.target_type is UnsubscribeHandle

        h.hub.publish("a", origin_scope=None)
        await _wait_until(lambda: sub.received == ["a"])
        h.hub.publish("b", origin_scope=None)
        await _wait_until(lambda: sub.received == ["a", "b"])


async def test_unsubscribe_releases() -> None:
    async with _hub_harness() as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        handle_ref = await h.proxy.subscribe(sub_ref)

        # The subscribe dispatch is long over by the time we use the
        # handle (risk item: "handle still works after the subscribe
        # dispatch ends").
        h.hub.publish(1, origin_scope=None)
        await _wait_until(lambda: sub.received == [1])

        handle = await handle_ref.resolve(h.caller_scope)
        await handle.unsubscribe()
        await _wait_until(lambda: h.hub.live_subscription_count == 0)

        h.hub.publish(2, origin_scope=None)
        await _settle()
        assert sub.received == [1]

        # Unsubscribe is idempotent while the handle is still held: the
        # holder scope is alive (draining) for as long as we pin it, so
        # a second call lands and no-ops.
        await handle.unsubscribe()


# ---------------------------------------------------------------------------
# Event residency: origin leases
# ---------------------------------------------------------------------------


async def test_entity_payload_delivers_from_drained_origin_and_releases() -> None:
    """Zero-copy publish: the payload entity lives in an origin scope
    that starts draining right after the publish; the captured lease
    keeps it loadable for the delivery, then the origin closes."""
    async with _hub_harness() as h:
        sub = _LoadingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        await h.proxy.subscribe(sub_ref)

        origin = _lowlevel_origin("publish_origin")
        try:
            ref = _Payload(text="hello").store(origin)
            h.hub.publish(ref.entity_id, origin_scope=origin)
            # Drop the owner's authorization: from here on only the
            # captured event lease keeps the origin open.
            origin.discard_base_authorization()
            await _wait_until(lambda: sub.loaded_texts == ["hello"])
            # Delivered → the per-delivery scope closed → the origin's
            # last lease was released and it finished draining.
            await _wait_until(lambda: not origin.is_open())
        finally:
            if origin.is_open():
                origin.force_close()


async def test_default_origin_is_active_scope() -> None:
    async with _hub_harness() as h:
        sub = _LoadingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        await h.proxy.subscribe(sub_ref)

        ref = _Payload(text="ambient").store(h.caller_scope)
        with h.caller_scope.as_active_scope():
            h.hub.publish(ref.entity_id)  # origin defaults to the active scope
        await _wait_until(lambda: sub.loaded_texts == ["ambient"])


async def test_superseded_coalesce_value_releases_origin_promptly() -> None:
    async with _hub_harness("coalesce") as h:
        sub = _RecordingSubscriberImpl()
        sub.release_gate = anyio.Semaphore(0)
        sub_ref = await h.mount_subscriber(sub)
        await h.proxy.subscribe(sub_ref)

        h.hub.publish("first", origin_scope=None)
        # on_event("first") has recorded its payload and is now blocked
        # on the gate, so subsequent publishes stay pending.
        await _wait_until(lambda: sub.received == ["first"])

        o2 = _lowlevel_origin("superseded_origin")
        o3 = _lowlevel_origin("pending_origin")
        try:
            h.hub.publish("second", origin_scope=o2)
            o2.discard_base_authorization()
            h.hub.publish("third", origin_scope=o3)
            o3.discard_base_authorization()
            # "second" was superseded before delivery: its origin lease
            # is discarded promptly, not at delivery time.
            await _wait_until(lambda: not o2.is_open())
            assert o3.is_open()  # "third" is still pending → still pinned

            sub.release_gate.release()  # let on_event("first") finish
            await _wait_until(lambda: sub.received == ["first", "third"])
            sub.release_gate.release()  # let on_event("third") finish
            # Delivery done → the per-event scope closed, releasing o3.
            await _wait_until(lambda: not o3.is_open())
        finally:
            for o in (o2, o3):
                if o.is_open():
                    o.force_close()


async def test_worker_teardown_releases_pending_origins() -> None:
    async with _hub_harness("queue") as h:
        sub = _RecordingSubscriberImpl()
        sub.release_gate = anyio.Semaphore(0)
        sub_ref = await h.mount_subscriber(sub)
        handle_ref = await h.proxy.subscribe(sub_ref)

        h.hub.publish("first", origin_scope=None)
        await _wait_until(lambda: sub.received == ["first"])  # in flight, gated

        origin = _lowlevel_origin("pending_origin")
        try:
            h.hub.publish("pending", origin_scope=origin)
            origin.discard_base_authorization()
            assert origin.is_open()  # pinned by the pending event

            handle = await handle_ref.resolve(h.caller_scope)
            await handle.unsubscribe()
            sub.release_gate.release()  # unblock the in-flight delivery
            await _wait_until(lambda: h.hub.live_subscription_count == 0)
            # The unsubscribe dropped the pending event — and its lease.
            await _wait_until(lambda: not origin.is_open())
            assert sub.received == ["first"]
        finally:
            if origin.is_open():
                origin.force_close()


async def test_publish_with_unauthorized_origin_raises() -> None:
    async with _hub_harness() as h:
        sub_ref = await h.mount_subscriber(_RecordingSubscriberImpl())
        await h.proxy.subscribe(sub_ref)
        origin = _lowlevel_origin("drained_origin")
        origin.discard_base_authorization()
        try:
            with pytest.raises(RuntimeError):
                h.hub.publish("x", origin_scope=origin)
        finally:
            origin.force_close()


async def test_broken_origin_lease_kills_subscription() -> None:
    """An origin force-closed while its event is still pending breaks
    the captured lease; the delivery failure is routed into the crash
    backstop (subscription dies, siblings unaffected) rather than
    skipping the event."""
    async with _hub_harness("coalesce") as h:
        gated = _RecordingSubscriberImpl()
        gated.release_gate = anyio.Semaphore(0)
        good = _RecordingSubscriberImpl()
        await h.proxy.subscribe(await h.mount_subscriber(gated))

        h.hub.publish("first", origin_scope=None)
        await _wait_until(lambda: gated.received == ["first"])  # in flight, gated

        origin = _lowlevel_origin("doomed_origin")
        h.hub.publish("doomed", origin_scope=origin)
        origin.force_close()  # breaks the pending event's lease

        # A sibling subscribed after the breakage keeps working.
        await h.proxy.subscribe(await h.mount_subscriber(good))

        gated.release_gate.release()  # let the worker reach the broken event
        await _wait_until(lambda: h.hub.live_subscription_count == 1)
        assert gated.received == ["first"]

        h.hub.publish("after", origin_scope=None)
        await _wait_until(lambda: good.received == ["after"])


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


async def test_seed_precedes_live_stream_queue() -> None:
    async with _hub_harness("queue") as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)

        h.hub.publish("gap", origin_scope=None)  # before subscribe: in neither
        await h.hub.subscribe(sub_ref, h.caller_scope, seed=["s1", "s2"], seed_origin=None)
        h.hub.publish("live", origin_scope=None)
        await _wait_until(lambda: sub.received == ["s1", "s2", "live"])


async def test_seed_collapses_in_coalesce_mode() -> None:
    async with _hub_harness("coalesce") as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        await h.hub.subscribe(sub_ref, h.caller_scope, seed=["s1", "s2"], seed_origin=None)
        await _wait_until(lambda: sub.received == ["s2"])
        await _settle()
        assert sub.received == ["s2"]


async def test_seed_origin_on_draining_scope_via_authorize() -> None:
    async with _hub_harness("queue") as h:
        sub = _LoadingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)

        origin = _lowlevel_origin("snapshot_origin")
        try:
            ref = _Payload(text="snap").store(origin)
            lease = origin.clone_authorization()
            origin.discard_base_authorization()  # draining: base is gone

            # Without an override the draining origin is unauthorized.
            with pytest.raises(RuntimeError):
                await h.hub.subscribe(
                    sub_ref, h.caller_scope, seed=[ref.entity_id], seed_origin=origin
                )

            with origin.authorize(lease):
                await h.hub.subscribe(
                    sub_ref, h.caller_scope, seed=[ref.entity_id], seed_origin=origin
                )
            await _wait_until(lambda: sub.loaded_texts == ["snap"])
            await _wait_until(lambda: not origin.is_open())
        finally:
            if origin.is_open():
                origin.force_close()


async def test_seed_through_dispatch_with_impl_scope_origin() -> None:
    """The agent-MCP shape: the source seeds each subscription with a
    snapshot stored in its own (non-active, non-call) impl scope."""
    snapshot_ref: list[FxtrID] = []

    def seed_provider() -> tuple[Sequence[Any], FxtrScope | None]:
        return list(snapshot_ref), seed_scope_holder[0]

    seed_scope_holder: list[FxtrScope | None] = [None]
    async with _hub_harness("queue", seed_provider=seed_provider) as h:
        seed_scope_holder[0] = h.source_mount
        snapshot_ref.append(_Payload(text="head").store(h.source_mount).entity_id)

        sub = _LoadingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        await h.proxy.subscribe(sub_ref)
        await _wait_until(lambda: sub.loaded_texts == ["head"])


async def test_entity_free_seed_with_none_origin() -> None:
    async with _hub_harness("queue") as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        await h.hub.subscribe(
            sub_ref, h.caller_scope, seed=[{"kind": "snapshot", "n": 1}], seed_origin=None
        )
        await _wait_until(lambda: sub.received == [{"kind": "snapshot", "n": 1}])


# ---------------------------------------------------------------------------
# Handle-drop teardown + holder lifecycle
# ---------------------------------------------------------------------------


async def test_handle_drop_tears_down_without_delivery() -> None:
    """A peer that releases its handle without unsubscribing tears the
    subscription down promptly — no failed delivery needed."""
    async with _hub_harness() as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)

        async with FxtrScope(name="dropper", include=[GLOBAL_SCOPE], isolated=True) as dropper:
            await dropper.bridge_service_from_scope(
                sub_ref, h.caller_scope, keepalive_via="independent_lease"
            )
            await h.hub.subscribe(sub_ref, dropper)
            assert h.hub.live_subscription_count == 1
        # dropper closed: the handle's only holder released it.
        await _wait_until(lambda: h.hub.live_subscription_count == 0)
        h.hub.publish("late", origin_scope=None)
        await _settle()
        assert sub.received == []


async def test_explicit_unsubscribe_completes_holder_naturally() -> None:
    async with _hub_harness() as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)

        async with FxtrScope(name="peer", include=[GLOBAL_SCOPE], isolated=True) as peer:
            await peer.bridge_service_from_scope(
                sub_ref, h.caller_scope, keepalive_via="independent_lease"
            )
            handle_ref = await h.hub.subscribe(sub_ref, peer)
            entry = h.hub._subscriptions[0]

            handle = await handle_ref.resolve(peer)
            await handle.unsubscribe()
            await _wait_until(lambda: h.hub.live_subscription_count == 0)
            # Worker is gone but the holder is parked: we still pin the
            # handle. No cancellation was requested.
            assert not entry.holder_cancel_requested
            assert not entry.holder_exited.is_set()
        # peer closed → handle released → holder drains to completion.
        await _wait_until(lambda: entry.holder_exited.is_set())
        assert not entry.holder_cancel_requested
        # The fully-exited entry is reaped on the next publish.
        h.hub.publish("x", origin_scope=None)
        assert h.hub._subscriptions == []


# ---------------------------------------------------------------------------
# aclose: flush, teardown, post-close errors
# ---------------------------------------------------------------------------


async def test_aclose_flushes_close_message_then_breaks_handles() -> None:
    async with _hub_harness("queue") as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        handle_ref = await h.proxy.subscribe(sub_ref)
        handle = await handle_ref.resolve(h.caller_scope)

        # The source-initiated close pattern: domain-level close
        # message (entity-free, origin=None — works from unauthorized
        # teardown contexts), then a flushing aclose.
        h.hub.publish("data", origin_scope=None)
        h.hub.publish({"kind": "closing"}, origin_scope=None)
        await h.hub.aclose()

        # Everything pending was delivered before the teardown...
        assert sub.received == ["data", {"kind": "closing"}]
        assert h.hub.live_subscription_count == 0

        # ...the peer's handle is now durably broken...
        with anyio.fail_after(5):
            with pytest.raises(Exception):
                await handle.unsubscribe()

        # ...and the hub rejects further use.
        with pytest.raises(RuntimeError):
            h.hub.publish("late", origin_scope=None)
        with pytest.raises(RuntimeError):
            await h.hub.subscribe(sub_ref, h.caller_scope)


async def test_aclose_with_wedged_subscriber_unwinds_on_cancel() -> None:
    """aclose's flush is unbounded; the bounded close is cancelling the
    owning task group, which must unwind the parked aclose cleanly."""
    with anyio.fail_after(10):
        async with FxtrScope(name="caller", include=[GLOBAL_SCOPE], isolated=True) as caller:
            sub = _RecordingSubscriberImpl()
            sub.release_gate = anyio.Semaphore(0)  # never released: wedged
            sub_ref = await caller.mount(sub)
            async with anyio.create_task_group() as tg:
                hub = SubscriberHub[FxtrServiceData](task_group=tg, mode="queue")
                await hub.subscribe(sub_ref, caller)
                hub.publish("x", origin_scope=None)
                await _wait_until(lambda: sub.received == ["x"])  # in flight, wedged
                hub.publish("never-delivered", origin_scope=None)

                async def closer() -> None:
                    await hub.aclose()

                tg.start_soon(closer)
                await _settle()  # let aclose park in the flush wait
                tg.cancel_scope.cancel()
            # The hub (workers, holders, the parked aclose) unwound on
            # cancellation. Release the orphaned in-flight dispatch so
            # the subscriber's mount scope can drain at CM exit — the
            # wedge was the subscriber's, not the hub's.
            sub.release_gate.release()


# ---------------------------------------------------------------------------
# Non-pinning topology (the headline property)
# ---------------------------------------------------------------------------


async def test_source_scope_drains_with_live_subscriptions() -> None:
    """A source's mount scope drains once its own holders are gone,
    even with live subscriptions outstanding — and those subscriptions
    keep delivering afterwards."""
    async with FxtrScope(name="caller", include=[GLOBAL_SCOPE], isolated=True) as caller:
        sub = _RecordingSubscriberImpl()
        sub_ref = await caller.mount(sub)
        async with anyio.create_task_group() as tg:
            hub = SubscriberHub[FxtrServiceData](task_group=tg)
            with anyio.fail_after(5):  # would deadlock if subscriptions pinned the source
                async with FxtrScope(
                    name="source_mount", include=[GLOBAL_SCOPE], isolated=True
                ) as source_mount:
                    impl = _HubSourceImpl(hub)
                    source_ref = await source_mount.mount(impl)
                    async with FxtrScope(
                        name="ref_holder", include=[GLOBAL_SCOPE], isolated=True
                    ) as ref_holder:
                        await ref_holder.bridge_service_from_scope(
                            source_ref, source_mount, keepalive_via="independent_lease"
                        )
                        await ref_holder.bridge_service_from_scope(
                            sub_ref, caller, keepalive_via="independent_lease"
                        )
                        proxy = await source_ref.resolve(ref_holder)
                        handle_ref = await proxy.subscribe(sub_ref)
                        # Keep the subscription alive past ref_holder:
                        # the handle is its keepalive, the source ref
                        # is not.
                        await caller.bridge_service_from_scope(
                            handle_ref, ref_holder, keepalive_via="independent_lease"
                        )
                    # ref_holder closed: the source ref's last holder
                    # is gone...
                # ...so source_mount drains promptly, live
                # subscription notwithstanding.
            assert hub.live_subscription_count == 1
            # The subscription outlives its source's mount scope.
            hub.publish("after-source-gone", origin_scope=None)
            await _wait_until(lambda: sub.received == ["after-source-gone"])
            tg.cancel_scope.cancel()


# ---------------------------------------------------------------------------
# Delivery modes
# ---------------------------------------------------------------------------


async def test_coalesce_skip_ahead_collapses() -> None:
    async with _hub_harness("coalesce") as h:
        sub = _RecordingSubscriberImpl()
        sub.release_gate = anyio.Semaphore(0)
        sub_ref = await h.mount_subscriber(sub)
        await h.proxy.subscribe(sub_ref)

        h.hub.publish(1, origin_scope=None)
        # on_event(1) has recorded its payload and is now blocked on
        # the gate.
        await _wait_until(lambda: sub.received == [1])
        h.hub.publish(2, origin_scope=None)
        h.hub.publish(3, origin_scope=None)
        h.hub.publish(4, origin_scope=None)
        sub.release_gate.release()  # let on_event(1) finish
        # 2 and 3 were superseded; only the newest value is delivered.
        await _wait_until(lambda: sub.received == [1, 4])
        sub.release_gate.release()  # let on_event(4) finish
        await _settle()
        assert sub.received == [1, 4]


async def test_queue_mode_delivers_every_event_in_order() -> None:
    async with _hub_harness("queue") as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        await h.proxy.subscribe(sub_ref)

        for i in range(1, 6):
            h.hub.publish(i, origin_scope=None)  # sync back-to-back: all queue up
        await _wait_until(lambda: sub.received == [1, 2, 3, 4, 5])


# ---------------------------------------------------------------------------
# Duplicate subscriptions + crash backstop
# ---------------------------------------------------------------------------


async def test_duplicate_subscribe_creates_independent_subscription() -> None:
    async with _hub_harness() as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        handle_ref_1 = await h.proxy.subscribe(sub_ref)
        handle_ref_2 = await h.proxy.subscribe(sub_ref)
        # No dedup: two independent subscriptions, two handles...
        assert h.hub.live_subscription_count == 2

        # ...so the subscriber receives every publish once per
        # subscription.
        h.hub.publish("x", origin_scope=None)
        await _wait_until(lambda: sub.received == ["x", "x"])
        await _settle()
        assert sub.received == ["x", "x"]

        # Each handle ends only its own subscription.
        handle_2 = await handle_ref_2.resolve(h.caller_scope)
        await handle_2.unsubscribe()
        await _wait_until(lambda: h.hub.live_subscription_count == 1)
        h.hub.publish("y", origin_scope=None)
        await _wait_until(lambda: sub.received == ["x", "x", "y"])

        handle_1 = await handle_ref_1.resolve(h.caller_scope)
        await handle_1.unsubscribe()
        await _wait_until(lambda: h.hub.live_subscription_count == 0)


async def test_resubscribe_after_unsubscribe_starts_fresh() -> None:
    async with _hub_harness() as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        handle = await (await h.proxy.subscribe(sub_ref)).resolve(h.caller_scope)
        await handle.unsubscribe()
        await _wait_until(lambda: h.hub.live_subscription_count == 0)

        # An ended (possibly not-yet-reaped) subscription doesn't get
        # in a fresh one's way.
        await h.proxy.subscribe(sub_ref)
        assert h.hub.live_subscription_count == 1
        h.hub.publish("again", origin_scope=None)
        await _wait_until(lambda: sub.received == ["again"])


async def test_on_event_raise_drops_subscription_but_not_siblings() -> None:
    async with _hub_harness() as h:
        bad = _RecordingSubscriberImpl(raise_on_event=True)
        good = _RecordingSubscriberImpl()
        await h.proxy.subscribe(await h.mount_subscriber(bad))
        await h.proxy.subscribe(await h.mount_subscriber(good))
        assert h.hub.live_subscription_count == 2

        h.hub.publish("x", origin_scope=None)
        await _wait_until(lambda: good.received == ["x"])
        # The raising subscriber's worker exits (crash backstop)...
        await _wait_until(lambda: h.hub.live_subscription_count == 1)
        # ...and the sibling keeps receiving.
        h.hub.publish("y", origin_scope=None)
        await _wait_until(lambda: good.received == ["x", "y"])
        assert bad.received == []


# ---------------------------------------------------------------------------
# Cancel teardown + direct (non-dispatch) hub use
# ---------------------------------------------------------------------------


async def test_task_group_cancel_tears_down_subscriptions() -> None:
    async with FxtrScope(name="caller", include=[GLOBAL_SCOPE], isolated=True) as caller:
        sub = _RecordingSubscriberImpl()
        sub_ref = await caller.mount(sub)
        hub_after_exit: SubscriberHub[FxtrServiceData] | None = None
        async with anyio.create_task_group() as tg:
            hub = SubscriberHub[FxtrServiceData](task_group=tg)
            hub_after_exit = hub
            # Direct hub use, no service dispatch in between:
            # source_scope is just the scope the subscriber ref
            # lives in.
            handle_ref = await hub.subscribe(sub_ref, caller)
            assert handle_ref.target_type is UnsubscribeHandle
            hub.publish("x", origin_scope=None)
            await _wait_until(lambda: sub.received == ["x"])
            assert hub.live_subscription_count == 1
            tg.cancel_scope.cancel()
        # Task-group exit ran every worker's teardown.
        assert hub_after_exit is not None
        assert hub_after_exit.live_subscription_count == 0
