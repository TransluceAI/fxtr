"""Shared test fixtures (impls + protocol definitions).

Lives under ``tests/`` because none of these are exported by the
package — they're test-only constructs that exercise the workstation's
generic surface.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Protocol

import anyio
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, ServiceRef, get_active_scope
from fxtr2.services.service_dialect import FxtrServiceData
from fxtr2.services.subscription_types import EventSource, Subscriber, UnsubscribeHandle
from fxtr2.services.subscription_util import SubscriberHub
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workstation.protocols import WorkstationSessionEventInbox

_ECHO_NS = uuid.UUID("11111111-2222-3333-4444-555555555555")
ECHO_TYPE_ID = system_entities.define_type(_ECHO_NS, "echo")

_NOTE_NS = uuid.UUID("22222222-3333-4444-5555-666666666666")
NOTE_TYPE_ID = system_entities.define_type(_NOTE_NS, "note")

_NOTEMAKER_NS = uuid.UUID("33333333-4444-5555-6666-777777777777")
NOTEMAKER_TYPE_ID = system_entities.define_type(_NOTEMAKER_NS, "notemaker")

_ECHO_FACTORY_NS = uuid.UUID("44444444-5555-6666-7777-888888888888")
ECHO_FACTORY_TYPE_ID = system_entities.define_type(_ECHO_FACTORY_NS, "echo_factory")

_NOTE_WITH_CHILD_NS = uuid.UUID("55555555-6666-7777-8888-999999999999")
NOTE_WITH_CHILD_TYPE_ID = system_entities.define_type(_NOTE_WITH_CHILD_NS, "note_with_child")

_SLOW_ECHO_NS = uuid.UUID("66666666-7777-8888-9999-aaaaaaaaaaaa")
SLOW_ECHO_TYPE_ID = system_entities.define_type(_SLOW_ECHO_NS, "slow_echo")

_CB_INVOKER_NS = uuid.UUID("77777777-8888-9999-aaaa-bbbbbbbbbbbb")
CB_INVOKER_TYPE_ID = system_entities.define_type(_CB_INVOKER_NS, "cb_invoker")

_HUB_SOURCE_NS = uuid.UUID("88888888-9999-aaaa-bbbb-cccccccccccc")
HUB_SOURCE_TYPE_ID = system_entities.define_type(_HUB_SOURCE_NS, "hub_event_source")
FAILING_SOURCE_TYPE_ID = system_entities.define_type(_HUB_SOURCE_NS, "failing_event_source")
WATCHABLE_SOURCE_TYPE_ID = system_entities.define_type(_HUB_SOURCE_NS, "watchable_source")
KEYED_SOURCE_TYPE_ID = system_entities.define_type(_HUB_SOURCE_NS, "keyed_source")


@service_protocol
class EchoProto(Protocol):
    """Trivial echo service used by the workstation tests."""

    async def echo(self, msg: str) -> str: ...

    async def echo_kw(self, *, msg: str, prefix: str = "kw") -> str: ...

    """``echo`` variant with keyword-only params — used to exercise
    the wire-level ``InvokeOp.kwargs`` plumbing. ``prefix`` is
    optional so we can also exercise the impl-default fall-through
    path."""


class EchoImpl(TypedServiceImpl[EchoProto]):
    """Default echo impl. Returns ``f"echo:{msg}"``."""

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return EchoProto

    def _service_type_id(self) -> FxtrID:
        return ECHO_TYPE_ID

    @service_method
    async def echo(self, msg: str) -> str:
        return f"echo:{msg}"

    @service_method
    async def echo_kw(self, *, msg: str, prefix: str = "kw") -> str:
        return f"{prefix}:{msg}"


@dataclasses.dataclass(frozen=True)
class Note(DataclassEntity, fxtr_type=NOTE_TYPE_ID):
    """Trivial entity used by the workstation entity tests."""

    title: str
    body: str


@service_protocol
class NoteMakerProto(Protocol):
    """Test service that returns entities and raises errors. Used by
    the 3c.3b invoke tests."""

    async def make_note(self, title: str, body: str) -> EntityRef[Note]: ...
    async def boom(self) -> None: ...


class NoteMakerImpl(TypedServiceImpl[NoteMakerProto]):
    """Stores a Note in the dispatch scope and returns the resulting
    ``EntityRef``. ``boom()`` raises ``RuntimeError("boom")``."""

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return NoteMakerProto

    def _service_type_id(self) -> FxtrID:
        return NOTEMAKER_TYPE_ID

    @service_method
    async def make_note(self, title: str, body: str) -> EntityRef[Note]:
        return Note(title=title, body=body).store()

    @service_method
    async def boom(self) -> None:
        raise RuntimeError("boom")


@dataclasses.dataclass(frozen=True)
class NoteWithChild(DataclassEntity, fxtr_type=NOTE_WITH_CHILD_TYPE_ID):
    """Test entity that holds an explicit child reference. Used to
    verify the load worker walks user-level entity refs (not just
    the ``$fxtr.type`` / ``$fxtr.ctx`` markers)."""

    title: str
    child: EntityRef[Note]


@service_protocol
class EchoFactoryProto(Protocol):
    """Test service that returns / accepts ``ServiceRef[EchoProto]``
    arguments. Used by the 3c invoke tests to exercise the
    service-from-result path, the service-arg path, and chained
    invokes."""

    async def make_echo(self) -> ServiceRef[EchoProto]: ...
    async def echo_via(self, echo: ServiceRef[EchoProto], msg: str) -> str: ...
    async def make_pair(self) -> dict[str, Any]: ...


class EchoFactoryImpl(TypedServiceImpl[EchoFactoryProto]):
    """``make_echo`` mounts a fresh :class:`EchoImpl` on the dispatch
    scope and returns its ref; ``echo_via`` resolves a passed-in
    echo ref and forwards a call; ``make_pair`` returns both an
    entity and a service in the same result (a dict so we don't
    have to fight tuple-typing)."""

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return EchoFactoryProto

    def _service_type_id(self) -> FxtrID:
        return ECHO_FACTORY_TYPE_ID

    @service_method
    async def make_echo(self) -> ServiceRef[EchoProto]:
        scope = get_active_scope()
        return (await scope.mount(EchoImpl())).replace_service_type(EchoProto)

    @service_method
    async def echo_via(self, echo: ServiceRef[EchoProto], msg: str) -> str:
        proxy = await echo.resolve()
        return await proxy.echo(msg)

    @service_method
    async def make_pair(self) -> dict[str, Any]:
        scope = get_active_scope()
        note = Note(title="paired", body="").store()
        echo_ref = (await scope.mount(EchoImpl())).replace_service_type(EchoProto)
        return {"note": note, "echo": echo_ref}


@service_protocol
class SlowEchoProto(Protocol):
    """Test service whose ``echo`` method sleeps before returning,
    used to engineer race scenarios (e.g. close-during-invoke)."""

    async def echo(self, msg: str, delay_seconds: float) -> str: ...


class SlowEchoImpl(TypedServiceImpl[SlowEchoProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return SlowEchoProto

    def _service_type_id(self) -> FxtrID:
        return SLOW_ECHO_TYPE_ID

    @service_method
    async def echo(self, msg: str, delay_seconds: float) -> str:
        await anyio.sleep(delay_seconds)
        return f"slow:{msg}"


@service_protocol
class CallbackInvokerProto(Protocol):
    """Test service used by the stage-5 callback tests. The peer
    that registers this is typically the test process itself; it
    receives a workstation-bridged
    ``ServiceRef[WorkstationSessionEventInbox]`` ref via ``fire`` and
    exercises ``notify`` / ``listening`` directly.

    All exercise happens within one dispatch — the bridged ref is
    only valid for the duration of the call_scope, so there's no
    "stash and call later" pattern. Tests that need multiple notifies
    use ``fire_many``.
    """

    async def fire(
        self, cb: ServiceRef[WorkstationSessionEventInbox], payload: FxtrServiceData
    ) -> None: ...
    async def fire_many(
        self, cb: ServiceRef[WorkstationSessionEventInbox], payloads: list[Any]
    ) -> int: ...
    async def is_listening(self, cb: ServiceRef[WorkstationSessionEventInbox]) -> bool: ...
    async def return_ref(
        self, cb: ServiceRef[WorkstationSessionEventInbox]
    ) -> ServiceRef[WorkstationSessionEventInbox]: ...


class CallbackInvokerImpl(TypedServiceImpl[CallbackInvokerProto]):
    """Resolves a ``ServiceRef[WorkstationSessionEventInbox]`` arg and
    exercises it. ``notify`` returns nothing (direct push is
    fire-and-forget), so ``fire`` returns ``None`` and ``fire_many``
    returns the count of pushes it made — the test asserts on those
    via the ``InvokeResultEvent`` payload."""

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return CallbackInvokerProto

    def _service_type_id(self) -> FxtrID:
        return CB_INVOKER_TYPE_ID

    @service_method
    async def fire(
        self, cb: ServiceRef[WorkstationSessionEventInbox], payload: FxtrServiceData
    ) -> None:
        proxy = await cb.resolve()
        await proxy.notify(payload)

    @service_method
    async def fire_many(
        self, cb: ServiceRef[WorkstationSessionEventInbox], payloads: list[Any]
    ) -> int:
        proxy = await cb.resolve()
        for payload in payloads:
            await proxy.notify(payload)
        return len(payloads)

    @service_method
    async def is_listening(self, cb: ServiceRef[WorkstationSessionEventInbox]) -> bool:
        proxy = await cb.resolve()
        return await proxy.listening()

    @service_method
    async def return_ref(
        self, cb: ServiceRef[WorkstationSessionEventInbox]
    ) -> ServiceRef[WorkstationSessionEventInbox]:
        """Round-trip the callback ref back through the invoke result.

        The workstation re-encodes it as ``["service", short_id]`` on
        the way out, surfacing the cb's send-side service short_id to
        the calling session — useful for cross-session passing tests
        and for the drained-but-alive test (which then reads the
        short_id and pulls the cb via ``get_service``)."""
        return cb


@service_protocol
class HubEventSourceProto(EventSource[FxtrServiceData], Protocol):
    """Test event source for the inbox ``subscribe_to`` tests.

    A genuine :class:`~fxtr2.services.subscription_types.EventSource`
    subclass; ``subscribe`` is inherited (the workstation re-types
    refs to ``EventSource`` and dispatches by method name).
    """


class HubEventSourceImpl(TypedServiceImpl[HubEventSourceProto]):
    """Hub-backed event source: ``subscribe`` delegates to a
    :class:`~fxtr2.services.subscription_util.SubscriberHub` the test
    constructs (and publishes through) directly."""

    def __init__(self, hub: SubscriberHub[FxtrServiceData]) -> None:
        self.hub = hub

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return HubEventSourceProto

    def _service_type_id(self) -> FxtrID:
        return HUB_SOURCE_TYPE_ID

    @service_method
    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]]
    ) -> ServiceRef[UnsubscribeHandle]:
        return await self.hub.subscribe(subscriber, get_active_scope())


@service_protocol
class FailingEventSourceProto(EventSource[FxtrServiceData], Protocol):
    """Event source whose ``subscribe`` always raises — used to verify
    ``subscribe_to`` failures surface as ``invoke_error``\\ s."""


class FailingEventSourceImpl(TypedServiceImpl[FailingEventSourceProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return FailingEventSourceProto

    def _service_type_id(self) -> FxtrID:
        return FAILING_SOURCE_TYPE_ID

    @service_method
    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]]
    ) -> ServiceRef[UnsubscribeHandle]:
        raise RuntimeError("this source rejects all subscribers")


@service_protocol
class WatchableSourceProto(Protocol):
    """Subscription source whose method is named ``watch`` (not
    ``subscribe``) and which is **not** an ``EventSource`` subclass —
    used to verify ``subscribe_to(source, method="watch")`` dispatches
    the source's subscription method by name."""

    async def watch(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]]
    ) -> ServiceRef[UnsubscribeHandle]: ...


class WatchableHubImpl(TypedServiceImpl[WatchableSourceProto]):
    """Hub-backed source exposing ``watch`` (same hub delegation as
    :class:`HubEventSourceImpl`, just under a different method name)."""

    def __init__(self, hub: SubscriberHub[FxtrServiceData]) -> None:
        self.hub = hub

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return WatchableSourceProto

    def _service_type_id(self) -> FxtrID:
        return WATCHABLE_SOURCE_TYPE_ID

    @service_method
    async def watch(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]]
    ) -> ServiceRef[UnsubscribeHandle]:
        return await self.hub.subscribe(subscriber, get_active_scope())


@service_protocol
class KeyedSourceProto(Protocol):
    """Subscription source whose method takes extra **keyword-only** args
    after the subscriber — used to verify
    ``subscribe_to(source, method, **kwargs)`` forwards them through the
    untyped dispatch into the source's typed decode."""

    async def watch_key(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]], *, key: str, depth: int
    ) -> ServiceRef[UnsubscribeHandle]: ...


class KeyedSourceImpl(TypedServiceImpl[KeyedSourceProto]):
    """Hub-backed source exposing ``watch_key(subscriber, *, key, depth)``;
    records each call's ``(key, depth)`` in ``seen`` for assertions."""

    def __init__(self, hub: SubscriberHub[FxtrServiceData]) -> None:
        self.hub = hub
        self.seen: list[tuple[str, int]] = []

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return KeyedSourceProto

    def _service_type_id(self) -> FxtrID:
        return KEYED_SOURCE_TYPE_ID

    @service_method
    async def watch_key(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]], *, key: str, depth: int
    ) -> ServiceRef[UnsubscribeHandle]:
        self.seen.append((key, depth))
        return await self.hub.subscribe(subscriber, get_active_scope())
