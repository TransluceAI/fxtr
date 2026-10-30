"""End-to-end tests for the typed-service layer.

Mirrors the ``service_layout`` fixture pattern from
``test_untyped_service.py``. Every parametrised test runs in three
configurations (same_scope / child_scope / sibling_attach), confirming
that typed services behave consistently across the cap-bridge layouts
the wider system supports.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol, TypeVar

import anyio
import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    Entity,
    EntityRef,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.services.service_cap_common import (
    USE_REMOTE_DEFAULT,
    FxtrServiceData,
    ServiceProtocolError,
    UnknownService,
)
from fxtr2.services.service_dialect import (
    FxtrServicesDefaultDialect,
    decode_from_service_data,
    encode_to_service_data,
)
from fxtr2.services.typed_service import (
    ServiceTypeIdMismatchError,
    TypedServiceImpl,
    proxy_alias_method,
    service_protocol,
)
from fxtr2.services.untyped_service import UntypedServiceImpl, service_method

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Module-level entity / type IDs
# ---------------------------------------------------------------------------


_TYPED_TYDEF_UUID = uuid.UUID("e6a3d2f1-0c7b-4a3e-9d8e-2f1c4a5b6e7d")
_NOTE_TYPE_ID = FxtrID.builtin("typed-test-note-svc")
_BOX_TYPE_ID = FxtrID.builtin("typed-test-box-svc")
_GREETER_TYPE_ID = FxtrID.builtin("typed-test-greeter-svc")
_DESC_TYPE_ID = FxtrID.builtin("typed-test-desc-svc")


@dataclasses.dataclass(frozen=True)
class _Note(DataclassEntity, fxtr_type=system_entities.define_type(_TYPED_TYDEF_UUID, "note")):
    body: str


@dataclasses.dataclass(frozen=True)
class _Greeting(
    DataclassEntity, fxtr_type=system_entities.define_type(_TYPED_TYDEF_UUID, "greeting")
):
    text: str


@dataclasses.dataclass(frozen=True)
class _ProtoDescriptor(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_TYPED_TYDEF_UUID, "proto_descriptor"),
):
    """Stand-in for a 'service descriptor' entity loaded via ``service_type_id``."""

    name: str
    version: int


# ---------------------------------------------------------------------------
# Protocol + Impl: plain primitives
# ---------------------------------------------------------------------------


@service_protocol
class GreeterProto(Protocol):
    async def greet(self, name: str, *, excited: bool = False) -> str: ...


class GreeterImpl(TypedServiceImpl[GreeterProto]):
    def _target_type_annotation(self) -> Any:
        return GreeterProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_GREETER_TYPE_ID)

    @service_method
    async def greet(self, name: str, *, excited: bool = False) -> str:
        return f"Hello, {name}!" + ("!" if excited else "")


# ---------------------------------------------------------------------------
# Protocol + Impl: EntityRef[T] / list of EntityRefs / FxtrID
# ---------------------------------------------------------------------------


@service_protocol
class NoteServiceProto(Protocol):
    async def read(self, ref: EntityRef[_Note]) -> str: ...

    async def first_body(self, refs: list[EntityRef[_Note]]) -> str: ...

    async def remember(self, body: str) -> EntityRef[_Note]: ...

    async def echo_id(self, fid: FxtrID) -> FxtrID: ...


class NoteServiceImpl(TypedServiceImpl[NoteServiceProto]):
    def _target_type_annotation(self) -> Any:
        return NoteServiceProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_NOTE_TYPE_ID)

    @service_method
    async def read(self, ref: EntityRef[_Note]) -> str:
        loaded = await ref.load()
        return loaded.body

    @service_method
    async def first_body(self, refs: list[EntityRef[_Note]]) -> str:
        loaded = await refs[0].load()
        return loaded.body

    @service_method
    async def remember(self, body: str) -> EntityRef[_Note]:
        return _Note(body=body).store()

    @service_method
    async def echo_id(self, fid: FxtrID) -> FxtrID:
        return fid


# ---------------------------------------------------------------------------
# Protocol + Impl: ServiceRef[T]
# ---------------------------------------------------------------------------


@service_protocol
class CallbackProto(Protocol):
    async def shout(self, what: str) -> str: ...


class CallbackImpl(TypedServiceImpl[CallbackProto]):
    def _target_type_annotation(self) -> Any:
        return CallbackProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-callback-svc"))

    @service_method
    async def shout(self, what: str) -> str:
        return what.upper()


@service_protocol
class CallbackUserProto(Protocol):
    async def receive(self, callback: ServiceRef[CallbackProto]) -> str: ...

    async def hand_back(self, callback: ServiceRef[CallbackProto]) -> ServiceRef[CallbackProto]: ...


class CallbackUserImpl(TypedServiceImpl[CallbackUserProto]):
    def _target_type_annotation(self) -> Any:
        return CallbackUserProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-callback-user-svc"))

    @service_method
    async def receive(self, callback: ServiceRef[CallbackProto]) -> str:
        proxy = await callback.resolve()
        return await proxy.shout("hi")

    @service_method
    async def hand_back(self, callback: ServiceRef[CallbackProto]) -> ServiceRef[CallbackProto]:
        return callback


# ---------------------------------------------------------------------------
# Protocol + Impl: collection types (frozenset / frozendict / tuple)
# ---------------------------------------------------------------------------


from frozendict import frozendict


@service_protocol
class CollectionsProto(Protocol):
    async def take_frozenset(self, xs: frozenset[int]) -> frozenset[int]: ...

    async def take_frozendict(self, d: frozendict[str, int]) -> frozendict[str, int]: ...  # type: ignore[type-arg]

    async def take_tuple(self, t: tuple[int, str]) -> tuple[str, int]: ...


class CollectionsImpl(TypedServiceImpl[CollectionsProto]):
    def _target_type_annotation(self) -> Any:
        return CollectionsProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-collections-svc"))

    @service_method
    async def take_frozenset(self, xs: frozenset[int]) -> frozenset[int]:
        return frozenset(x * 2 for x in xs)

    @service_method
    async def take_frozendict(self, d: frozendict[str, int]) -> frozendict[str, int]:  # type: ignore[type-arg]
        return frozendict({k: v + 1 for k, v in d.items()})

    @service_method
    async def take_tuple(self, t: tuple[int, str]) -> tuple[str, int]:
        return (t[1], t[0])


# ---------------------------------------------------------------------------
# Protocols exposing service_type_id via @proxy_alias_method("$fxtr.type")
# ---------------------------------------------------------------------------


@service_protocol
class WithEntityRefSTIProto(Protocol):
    @proxy_alias_method("$fxtr.type")
    async def service_type_id(self) -> EntityRef[_ProtoDescriptor]: ...

    async def ping(self) -> str: ...


class WithEntityRefSTIImpl(TypedServiceImpl[WithEntityRefSTIProto]):
    def _target_type_annotation(self) -> Any:
        return WithEntityRefSTIProto

    def _service_type_id(self) -> FxtrID:
        # The mount scope is already the active scope (FxtrScope.mount
        # sets this), so a plain `.store()` lands the descriptor in
        # the right place — exactly the same pattern as
        # Entity._fxtr_store_impl.
        return _ProtoDescriptor(name="with-entity-ref-sti", version=1).store().entity_id

    @service_method
    async def ping(self) -> str:
        return "pong"


@service_protocol
class WithFxtrIdSTIProto(Protocol):
    @proxy_alias_method("$fxtr.type")
    async def service_type_id(self) -> FxtrID: ...

    async def ping(self) -> str: ...


class WithFxtrIdSTIImpl(TypedServiceImpl[WithFxtrIdSTIProto]):
    def _target_type_annotation(self) -> Any:
        return WithFxtrIdSTIProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-with-fxtr-id-sti"))

    @service_method
    async def ping(self) -> str:
        return "pong"


# ---------------------------------------------------------------------------
# Service-type-id gating: equality + validator
# ---------------------------------------------------------------------------


_GATED_EQ_ID = FxtrID.builtin("typed-gated-eq")
_GATED_OK_ID = FxtrID.builtin("typed-gated-ok")
_GATED_NO_ID = FxtrID.builtin("typed-gated-no")


@service_protocol(service_type_id=_GATED_EQ_ID)
class GatedEqProto(Protocol):
    async def ping(self) -> str: ...


class GatedEqImpl(TypedServiceImpl[GatedEqProto]):
    """Impl matching the gate."""

    def _target_type_annotation(self) -> Any:
        return GatedEqProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_GATED_EQ_ID)

    @service_method
    async def ping(self) -> str:
        return "ok"


class WrongIdImpl(TypedServiceImpl[GatedEqProto]):
    """Impl whose service_type_id deliberately doesn't match the gate."""

    def _target_type_annotation(self) -> Any:
        return GatedEqProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-gated-wrong"))

    @service_method
    async def ping(self) -> str:
        return "ok"


async def _validator(service_type_id: FxtrID, target_service_type: Any) -> bool:  # noqa: ARG001
    return service_type_id == _GATED_OK_ID


@service_protocol(service_type_validator=_validator)
class ValidatorProto(Protocol):
    async def ping(self) -> str: ...


class ValidatorOkImpl(TypedServiceImpl[ValidatorProto]):
    def _target_type_annotation(self) -> Any:
        return ValidatorProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_GATED_OK_ID)

    @service_method
    async def ping(self) -> str:
        return "ok"


class ValidatorNoImpl(TypedServiceImpl[ValidatorProto]):
    def _target_type_annotation(self) -> Any:
        return ValidatorProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_GATED_NO_ID)

    @service_method
    async def ping(self) -> str:
        return "ok"


# ---------------------------------------------------------------------------
# Class-level generics on the impl side
# ---------------------------------------------------------------------------


T_BOX = TypeVar("T_BOX")


@service_protocol
class BoxProto(Protocol[T_BOX]):
    async def get(self) -> T_BOX: ...


class GenericBoxImpl[T](TypedServiceImpl[BoxProto[T]]):
    """Generic impl: stash type arg + value, override
    ``_own_full_type_for_serialization`` to return the parameterized form.
    """

    def __init__(self, type_arg: type, value: Any) -> None:
        self._type_arg = type_arg
        self._value = value

    def _target_type_annotation(self) -> Any:
        return BoxProto[self._type_arg]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_BOX_TYPE_ID)

    def _own_full_type_for_serialization(self) -> Any:
        return GenericBoxImpl[self._type_arg]

    @service_method
    async def get(self) -> T:
        return self._value


# ---------------------------------------------------------------------------
# Layout fixture (mirrors test_untyped_service.py)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Layout:
    service_scope: FxtrScope
    caller_scope: FxtrScope


@asynccontextmanager
async def _layout_same_scope() -> AsyncIterator[_Layout]:
    async with FxtrScope(name="same", include=[GLOBAL_SCOPE], isolated=True) as scope:
        yield _Layout(service_scope=scope, caller_scope=scope)


@asynccontextmanager
async def _layout_child_scope() -> AsyncIterator[_Layout]:
    async with FxtrScope(name="parent", include=[GLOBAL_SCOPE], isolated=True) as parent:
        async with FxtrScope(name="child", include=[parent], isolated=True) as child:
            yield _Layout(service_scope=parent, caller_scope=child)


@asynccontextmanager
async def _layout_sibling_attach() -> AsyncIterator[_Layout]:
    async with FxtrScope(name="svc", include=[GLOBAL_SCOPE], isolated=True) as svc:
        async with FxtrScope(name="caller", include=[GLOBAL_SCOPE], isolated=True) as caller:
            yield _Layout(service_scope=svc, caller_scope=caller)


ServiceLayoutFactory = Callable[[], AbstractAsyncContextManager[_Layout]]


_LAYOUTS: dict[str, ServiceLayoutFactory] = {
    "same_scope": _layout_same_scope,
    "child_scope": _layout_child_scope,
    "sibling_attach": _layout_sibling_attach,
}


@pytest.fixture(params=list(_LAYOUTS.keys()))
def service_layout(request: pytest.FixtureRequest) -> ServiceLayoutFactory:
    return _LAYOUTS[request.param]


async def _make_ref_visible_from_caller[T](layout: _Layout, ref: ServiceRef[T]) -> ServiceRef[T]:
    """Bridge a ref into the caller scope when it isn't reachable via parent walk."""
    if layout.service_scope is layout.caller_scope:
        return ref
    if layout.service_scope in layout.caller_scope._parents:
        return ref
    # Sibling: explicit cross-tree bridge. The bridge stays registered
    # on caller_scope until it drains.
    await layout.caller_scope.bridge_service_from_scope(
        ref, layout.service_scope, keepalive_via="independent_lease"
    )
    return ref


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestPlainPrimitives:
    async def test_basic_call(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(GreeterImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.greet("world") == "Hello, world!"
            assert await proxy.greet("world", excited=True) == "Hello, world!!"


class TestEntityRef:
    async def test_entity_ref_arg_and_return(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(NoteServiceImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            with layout.caller_scope.as_active_scope():
                note_ref = _Note(body="hello").store()
                assert await proxy.read(note_ref) == "hello"
                assert await proxy.first_body([note_ref]) == "hello"
                got_back = await proxy.remember("from-impl")
                assert isinstance(got_back, EntityRef)
                assert got_back.target_type is _Note
                loaded = await got_back.load()
                assert loaded.body == "from-impl"

    async def test_fxtr_id_round_trip(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(NoteServiceImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            with layout.caller_scope.as_active_scope():
                note_ref = _Note(body="x").store()
                fid = note_ref.entity_id
                got = await proxy.echo_id(fid)
                assert got == fid


class TestServiceRefArgsAndReturns:
    async def test_serviceref_arg_resolved_on_impl(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            cb_ref = await layout.service_scope.mount(CallbackImpl())
            user_ref = await layout.service_scope.mount(CallbackUserImpl())
            visible_user = await _make_ref_visible_from_caller(layout, user_ref)
            visible_cb = await _make_ref_visible_from_caller(layout, cb_ref)
            proxy = await visible_user.resolve(layout.caller_scope)
            assert await proxy.receive(visible_cb) == "HI"

    async def test_serviceref_round_trip(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            cb_ref = await layout.service_scope.mount(CallbackImpl())
            user_ref = await layout.service_scope.mount(CallbackUserImpl())
            visible_user = await _make_ref_visible_from_caller(layout, user_ref)
            visible_cb = await _make_ref_visible_from_caller(layout, cb_ref)
            proxy = await visible_user.resolve(layout.caller_scope)
            handed_back = await proxy.hand_back(visible_cb)
            assert isinstance(handed_back, ServiceRef)
            # Target type was retyped against the protocol's annotation
            # (CallbackProto), even though the wire form ships
            # ServiceRef[UnknownService].
            assert handed_back.target_type is CallbackProto
            cb_proxy = await handed_back.resolve(layout.caller_scope)
            assert await cb_proxy.shout("ping") == "PING"


class TestCollections:
    async def test_frozenset(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(CollectionsImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.take_frozenset(frozenset({1, 2, 3})) == frozenset({2, 4, 6})

    async def test_frozendict(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(CollectionsImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            got = await proxy.take_frozendict(frozendict({"a": 1, "b": 2}))
            assert got == frozendict({"a": 2, "b": 3})

    async def test_tuple(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(CollectionsImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.take_tuple((1, "two")) == ("two", 1)


class TestServiceTypeIdProperty:
    """Type-id introspection on typed proxies via
    ``@proxy_alias_method("$fxtr.type")`` — opt-in per Protocol.
    A Protocol that doesn't declare an alias method has no proxy-side
    type-id slot at all (callers can still use the raw wire path via
    ``invoke_service_method(ctx, "$fxtr.type")``).
    """

    async def test_entity_ref_property(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(WithEntityRefSTIImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            sti = await proxy.service_type_id()
            assert isinstance(sti, EntityRef)
            assert sti.target_type is _ProtoDescriptor
            with layout.caller_scope.as_active_scope():
                # The return-arena bridges the descriptor's id into
                # caller_scope, so EntityRef.load() works directly.
                loaded = await sti.load()
                assert loaded.name == "with-entity-ref-sti"
                assert loaded.version == 1

    async def test_fxtr_id_property(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(WithFxtrIdSTIImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.service_type_id() == FxtrID.builtin("typed-with-fxtr-id-sti")

    async def test_absent_alias_no_attribute(self, service_layout: ServiceLayoutFactory) -> None:
        """A Protocol without a `@proxy_alias_method("$fxtr.type")` method
        has no `service_type_id` slot on the proxy at all."""
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(GreeterImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert not hasattr(proxy, "service_type_id")
            assert not hasattr(proxy, "_service_type_id")


class TestServiceTypeIdGating:
    async def test_equality_pass(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(GatedEqImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.ping() == "ok"

    async def test_equality_fail(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(WrongIdImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            with pytest.raises(ServiceTypeIdMismatchError):
                await visible.resolve(layout.caller_scope)

    async def test_validator_pass(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(ValidatorOkImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.ping() == "ok"

    async def test_validator_fail(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(ValidatorNoImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            with pytest.raises(ServiceTypeIdMismatchError):
                await visible.resolve(layout.caller_scope)


class TestServiceTypeValidatorCache:
    """The validator path caches results per `(validator, wire_type_id)`:
    cache miss → invoke `$fxtr.type` once → run predicate → cache.
    Cache hit → trust wire hash → no `$fxtr.type` dispatch and no
    second predicate call.
    """

    async def test_validator_called_once_per_distinct_type_id(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        # Counting validator wired into a fresh local protocol so the
        # module-level cache for `_validator` doesn't pollute the count.
        from fxtr2.services.typed_service import _VALIDATOR_RESULT_CACHE

        call_counter = {"n": 0}

        async def _counting_validator(
            service_type_id: FxtrID,
            target_service_type: Any,  # noqa: ARG001
        ) -> bool:
            call_counter["n"] += 1
            return service_type_id == _GATED_OK_ID

        @service_protocol(service_type_validator=_counting_validator)
        class _CountedProto(Protocol):
            async def ping(self) -> str: ...

        class _OkImpl(TypedServiceImpl[_CountedProto]):
            def _target_type_annotation(self) -> Any:
                return _CountedProto

            def _service_type_id(self) -> FxtrID:
                return get_active_scope().track_builtin(_GATED_OK_ID)

            @service_method
            async def ping(self) -> str:
                return "ok"

        try:
            async with service_layout() as layout:
                # First resolve: cache miss → validator called once.
                ref1 = await layout.service_scope.mount(_OkImpl())
                visible1 = await _make_ref_visible_from_caller(layout, ref1)
                proxy1 = await visible1.resolve(layout.caller_scope)
                assert await proxy1.ping() == "ok"
                assert call_counter["n"] == 1

                # Second resolve with the SAME impl + same type_id:
                # cache hit, validator not called again.
                ref2 = await layout.service_scope.mount(_OkImpl())
                visible2 = await _make_ref_visible_from_caller(layout, ref2)
                proxy2 = await visible2.resolve(layout.caller_scope)
                assert await proxy2.ping() == "ok"
                assert call_counter["n"] == 1
        finally:
            # Don't leave entries in the module-level cache.
            _VALIDATOR_RESULT_CACHE.pop(_counting_validator, None)

    async def test_validator_receives_loadable_type_id(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        """On cache miss the resolver dispatches `$fxtr.type` so the
        validator gets the id in loadable form — actually loadable as
        the descriptor entity it points at, not just an opaque hash.
        The validator does the load itself, exercising the loadable
        contract from inside the gating predicate (which is the whole
        point of the validator round-trip).
        """
        from fxtr2.services.typed_service import _VALIDATOR_RESULT_CACHE

        seen_descriptors: list[_ProtoDescriptor] = []

        async def _loading_validator(
            service_type_id: FxtrID,
            target_service_type: Any,  # noqa: ARG001
        ) -> bool:
            # Actually load the descriptor through the id we got —
            # proves the id is loadable, not just is_tracked.
            descriptor = await EntityRef(service_type_id, _ProtoDescriptor).load()
            seen_descriptors.append(descriptor)
            return descriptor.name == "cap-validator"

        @service_protocol(service_type_validator=_loading_validator)
        class _CapturedProto(Protocol):
            async def ping(self) -> str: ...

        class _CapturedImpl(TypedServiceImpl[_CapturedProto]):
            def _target_type_annotation(self) -> Any:
                return _CapturedProto

            def _service_type_id(self) -> FxtrID:
                return _ProtoDescriptor(name="cap-validator", version=1).store().entity_id

            @service_method
            async def ping(self) -> str:
                return "ok"

        try:
            async with service_layout() as layout:
                ref = await layout.service_scope.mount(_CapturedImpl())
                visible = await _make_ref_visible_from_caller(layout, ref)
                # ``EntityRef.load()`` inside the validator uses the
                # active scope; resolve under caller_scope-active so
                # the load lands on the same scope the return-arena
                # bridged the descriptor into.
                with layout.caller_scope.as_active_scope():
                    _ = await visible.resolve(layout.caller_scope)
                assert len(seen_descriptors) == 1
                loaded = seen_descriptors[0]
                assert loaded.name == "cap-validator"
                assert loaded.version == 1
        finally:
            _VALIDATOR_RESULT_CACHE.pop(_loading_validator, None)


class TestImplGenerics:
    async def test_int_box(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            impl = GenericBoxImpl(int, 42)
            ref = await layout.service_scope.mount(impl)
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.get() == 42

    async def test_str_box(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            impl = GenericBoxImpl(str, "hello")
            ref = await layout.service_scope.mount(impl)
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.get() == "hello"


class TestRegistrationErrors:
    def test_method_level_typevar_rejected(self) -> None:
        T2 = TypeVar("T2")
        with pytest.raises(ServiceProtocolError):

            @service_protocol
            class _BadProto(Protocol):
                async def gen(self) -> T2: ...  # type: ignore[valid-type, misc]

    def test_sync_service_method_rejected(self) -> None:
        with pytest.raises(ServiceProtocolError):

            class _BadImpl(TypedServiceImpl[GreeterProto]):
                def _target_type_annotation(self) -> Any:
                    return GreeterProto

                def _service_type_id(self) -> FxtrID:
                    return get_active_scope().track_builtin(FxtrID.builtin("bad"))

                @service_method  # type: ignore[arg-type]
                def greet(self, name: str) -> str:  # noqa: D401 — sync on purpose
                    return name

    def test_dunder_service_method_rejected(self) -> None:
        with pytest.raises(ServiceProtocolError):

            class _BadImpl(TypedServiceImpl[GreeterProto]):
                def _target_type_annotation(self) -> Any:
                    return GreeterProto

                def _service_type_id(self) -> FxtrID:
                    return get_active_scope().track_builtin(FxtrID.builtin("bad"))

                @service_method
                async def __my_dunder__(self) -> str:  # type: ignore[misc]
                    return "no"

    def test_mutually_exclusive_service_type_args(self) -> None:
        with pytest.raises(ServiceProtocolError):

            @service_protocol(
                service_type_id=FxtrID.builtin("x"),
                service_type_validator=_validator,
            )
            class _BadProto(Protocol):
                async def ping(self) -> str: ...


class TestImplGenericsErrors:
    async def test_unbound_typevar_rejected(self) -> None:
        async with FxtrScope(name="root", isolated=True) as scope:

            class BadGenericImpl[T](TypedServiceImpl[BoxProto[T]]):
                def _target_type_annotation(self) -> Any:
                    return BoxProto[Any]

                def _service_type_id(self) -> FxtrID:
                    return get_active_scope().track_builtin(FxtrID.builtin("bad-generic"))

                # Default _own_full_type_for_serialization returns
                # type(self), which has unbound TypeVars → mount fails.

                @service_method
                async def get(self) -> Any:
                    return None

            with pytest.raises(ServiceProtocolError):
                await scope.mount(BadGenericImpl())


# ---------------------------------------------------------------------------
# Default-handling tests
# ---------------------------------------------------------------------------


# Concrete-default protocols/impls used to verify that proxy/impl defaults
# work as expected (Protocol's default applied at proxy call time, impl's
# default applied when proxy passes USE_REMOTE_DEFAULT or omits the arg).


@service_protocol
class _ConcreteDefaultProto(Protocol):
    async def echo(self, x: int = 5) -> int: ...


class _ConcreteDefaultImpl(TypedServiceImpl[_ConcreteDefaultProto]):
    def _target_type_annotation(self) -> Any:
        return _ConcreteDefaultProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-cd-svc"))

    @service_method
    async def echo(self, x: int = 99) -> int:
        return x


@service_protocol
class _DelegatedDefaultProto(Protocol):
    async def echo(self, x: int = USE_REMOTE_DEFAULT) -> int: ...


class _DelegatedDefaultImpl(TypedServiceImpl[_DelegatedDefaultProto]):
    def _target_type_annotation(self) -> Any:
        return _DelegatedDefaultProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-dd-svc"))

    @service_method
    async def echo(self, x: int = 99) -> int:
        return x


@service_protocol
class _MutableDefaultProto(Protocol):
    """Protocol with a mutable default — shouldn't crash at decoration time."""

    async def echo(self, items: list[int] = [1, 2, 3]) -> list[int]: ...  # noqa: B006


class _MutableDefaultImpl(TypedServiceImpl[_MutableDefaultProto]):
    def _target_type_annotation(self) -> Any:
        return _MutableDefaultProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-mut-svc"))

    @service_method
    async def echo(self, items: list[int] = [10, 20]) -> list[int]:  # noqa: B006
        return items


# Forward-compat: impl has fields the proxy doesn't know about (with defaults).


@service_protocol
class _OldProto(Protocol):
    async def greet(self, name: str) -> str: ...


class _ExtendedImpl(TypedServiceImpl[_OldProto]):
    """Impl declares an extra optional parameter the old Protocol doesn't have.

    Old proxies can still call this — the wire ships only ``name``,
    the impl-side wrapper field for ``greeting`` defaults to
    ``USE_REMOTE_DEFAULT``, and the dispatcher strips it so Python's
    call machinery applies the impl's own default of ``"Hi"``.
    """

    def _target_type_annotation(self) -> Any:
        return _OldProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-ext-svc"))

    @service_method
    async def greet(self, name: str, greeting: str = "Hi") -> str:
        return f"{greeting}, {name}!"


# Strict-extras: proxy ships a field the impl doesn't know — should error.


@service_protocol
class _NewerProto(Protocol):
    async def greet(self, name: str, lang: str = "en") -> str: ...


class _OlderImpl(TypedServiceImpl[_NewerProto]):
    """Impl signature is *narrower* than the Protocol's. The Protocol's
    Protocol-side default for ``lang`` will get baked into the wire by
    apply_defaults; the impl's wrapper rejects the unknown ``lang`` field.
    """

    def _target_type_annotation(self) -> Any:
        return _NewerProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-old-svc"))

    @service_method
    async def greet(self, name: str) -> str:
        return f"Hello, {name}!"


# Proto/impl with *args / **kwargs for nested-sentinel checks.


@service_protocol
class _VariadicProto(Protocol):
    async def collect(self, *items: int, **flags: int) -> int: ...


class _VariadicImpl(TypedServiceImpl[_VariadicProto]):
    def _target_type_annotation(self) -> Any:
        return _VariadicProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-var-svc"))

    @service_method
    async def collect(self, *items: int, **flags: int) -> int:
        return sum(items) + sum(flags.values())


class TestProtocolDefaults:
    async def test_protocol_default_applied_at_call_time(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_ConcreteDefaultImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            # Protocol default is 5; impl default is 99. Protocol wins
            # because apply_defaults bakes the Protocol's default into
            # the wire.
            assert await proxy.echo() == 5

    async def test_explicit_user_value_overrides_default(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_ConcreteDefaultImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.echo(x=42) == 42


class TestUseRemoteDefault:
    async def test_proxy_use_remote_default_falls_through_to_impl(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_DelegatedDefaultImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            # Protocol default is USE_REMOTE_DEFAULT → wire ships nothing
            # for x → impl applies its own default of 99.
            assert await proxy.echo() == 99

    async def test_explicit_use_remote_default_falls_through(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_ConcreteDefaultImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            # User explicitly passes USE_REMOTE_DEFAULT — same effect as
            # omitting on a USE_REMOTE_DEFAULT-defaulted Protocol.
            assert await proxy.echo(x=USE_REMOTE_DEFAULT) == 99


class TestMutableDefaults:
    async def test_proxy_mutable_default_works(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_MutableDefaultImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.echo() == [1, 2, 3]

    async def test_proxy_user_supplied_overrides_mutable_default(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_MutableDefaultImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.echo(items=[7, 8]) == [7, 8]


class TestForwardCompat:
    async def test_old_proxy_against_extended_impl(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        # Old Protocol has only ``name``; impl has extra ``greeting`` with default.
        # Wire ships only ``name``; impl's default fires.
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_ExtendedImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.greet(name="world") == "Hi, world!"

    async def test_new_proxy_against_older_impl_strict_error(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        # New Protocol has extra ``lang`` field; impl doesn't know about it.
        # Wire ships ``name`` + ``lang``; impl's wrapper rejects the unknown field.
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_OlderImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            with pytest.raises(IRException):
                await proxy.greet(name="world")


class TestNestedSentinelRejected:
    """USE_REMOTE_DEFAULT only makes sense as a top-level named-parameter
    default — nested inside ``*args`` / ``**kwargs`` / a collection it would
    end up as a literal element on the wire. We don't validate this in the
    typed-service layer; the underlying ``FxtrServiceData`` vocabulary check
    in :func:`invoke_service_method` rejects the sentinel naturally
    (it's not in the wire's primitive vocabulary)."""

    async def test_sentinel_in_var_positional_rejected(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_VariadicImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            with pytest.raises(TypeError) as exc_info:
                await proxy.collect(1, USE_REMOTE_DEFAULT, 3)
            assert "USE_REMOTE_DEFAULT" in str(exc_info.value)

    async def test_sentinel_in_var_keyword_rejected(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_VariadicImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            with pytest.raises(TypeError) as exc_info:
                await proxy.collect(1, 2, x=USE_REMOTE_DEFAULT)
            assert "USE_REMOTE_DEFAULT" in str(exc_info.value)

    async def test_sentinel_nested_in_collection_rejected(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_MutableDefaultImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            with pytest.raises(TypeError) as exc_info:
                # USE_REMOTE_DEFAULT inside a list value: nested, not allowed.
                await proxy.echo(items=[1, USE_REMOTE_DEFAULT, 3])
            assert "USE_REMOTE_DEFAULT" in str(exc_info.value)


class TestRequiredFieldMissing:
    async def test_impl_required_field_missing_raises(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        @service_protocol
        class _ProtoOptional(Protocol):
            async def go(self, x: int = USE_REMOTE_DEFAULT) -> int: ...

        class _ImplRequired(TypedServiceImpl[_ProtoOptional]):
            def _target_type_annotation(self) -> Any:
                return _ProtoOptional

            def _service_type_id(self) -> FxtrID:
                return get_active_scope().track_builtin(FxtrID.builtin("typed-req-svc"))

            @service_method
            async def go(self, x: int) -> int:  # required, no default
                return x * 2

        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_ImplRequired())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            # Wire ships nothing for x (Protocol default is sentinel,
            # stripped); impl decode fails because x is required there.
            with pytest.raises(IRException):
                await proxy.go()


# ---------------------------------------------------------------------------
# Positional-only round-trip + USE_REMOTE_DEFAULT suffix rule
# ---------------------------------------------------------------------------


@service_protocol
class _PositionalOnlyProto(Protocol):
    async def add(self, a: int, b: int, /) -> int: ...

    async def add3(
        self,
        a: int,
        b: int = USE_REMOTE_DEFAULT,
        c: int = USE_REMOTE_DEFAULT,
        /,
    ) -> int: ...


class _PositionalOnlyImpl(TypedServiceImpl[_PositionalOnlyProto]):
    def _target_type_annotation(self) -> Any:
        return _PositionalOnlyProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-posonly-svc"))

    @service_method
    async def add(self, a: int, b: int, /) -> int:
        return a + b

    @service_method
    async def add3(self, a: int, b: int = 100, c: int = 1000, /) -> int:
        return a + b + c


@service_protocol
class _MixedProto(Protocol):
    """``self, pos_only, /, pos_or_kw, *items, kw_only=...``."""

    async def call(
        self,
        a: int,
        /,
        b: int,
        *items: int,
        flag: bool = False,
    ) -> tuple[int, int, tuple[int, ...], bool]: ...


class _MixedImpl(TypedServiceImpl[_MixedProto]):
    def _target_type_annotation(self) -> Any:
        return _MixedProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-mixed-svc"))

    @service_method
    async def call(
        self,
        a: int,
        /,
        b: int,
        *items: int,
        flag: bool = False,
    ) -> tuple[int, int, tuple[int, ...], bool]:
        return (a, b, items, flag)


class TestPositionalOnly:
    async def test_round_trip(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_PositionalOnlyImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.add(2, 3) == 5

    async def test_trailing_use_remote_default_suffix(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_PositionalOnlyImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            # All three concrete: 1+2+3 = 6.
            assert await proxy.add3(1, 2, 3) == 6
            # Trailing default for c: 1+2+1000 = 1003.
            assert await proxy.add3(1, 2) == 1003
            # Both b and c default: 1+100+1000 = 1101.
            assert await proxy.add3(1) == 1101

    async def test_explicit_use_remote_default_in_suffix(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_PositionalOnlyImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            # User explicitly passes USE_REMOTE_DEFAULT for the trailing
            # positional-only param — same effect as omitting.
            assert await proxy.add3(1, 2, USE_REMOTE_DEFAULT) == 1003
            assert await proxy.add3(1, USE_REMOTE_DEFAULT, USE_REMOTE_DEFAULT) == 1101

    async def test_use_remote_default_non_suffix_rejected(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_PositionalOnlyImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            # USE_REMOTE_DEFAULT for b followed by concrete c — not a
            # suffix, can't be reconstructed positionally on the impl
            # side. The wire ships c as a kwarg (because dropping b
            # makes BoundArguments switch to .kwargs at b), and the
            # impl's call raises "got some positional-only arguments
            # passed as keyword arguments".
            with pytest.raises(IRException) as exc_info:
                await proxy.add3(1, USE_REMOTE_DEFAULT, 3)
            assert "positional-only" in str(exc_info.value)


class TestMixedPositionalAndVariadic:
    async def test_round_trip_no_args(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_MixedImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.call(1, 2) == (1, 2, (), False)
            assert await proxy.call(1, 2, flag=True) == (1, 2, (), True)

    async def test_round_trip_with_var_positional(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_MixedImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            # *items receives (10, 20, 30); a=1 (positional-only), b=2.
            assert await proxy.call(1, 2, 10, 20, 30) == (1, 2, (10, 20, 30), False)
            assert await proxy.call(1, 2, 10, 20, 30, flag=True) == (1, 2, (10, 20, 30), True)

    async def test_b_passed_by_keyword(self, service_layout: ServiceLayoutFactory) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_MixedImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            # b is positional-or-keyword. Without *items content, it's
            # safe to pass by keyword.
            assert await proxy.call(1, b=2) == (1, 2, (), False)


class TestVariadicRoundTrip:
    """Plain *args / **kwargs round-trip — the bug we discovered while
    fixing the impl-side bind. Previously the *items content silently
    landed in **flags because bind(items=tuple) routed by name."""

    async def test_var_positional_only(self, service_layout: ServiceLayoutFactory) -> None:
        @service_protocol
        class _VarPosProto(Protocol):
            async def sum_all(self, *items: int) -> int: ...

        class _VarPosImpl(TypedServiceImpl[_VarPosProto]):
            def _target_type_annotation(self) -> Any:
                return _VarPosProto

            def _service_type_id(self) -> FxtrID:
                return get_active_scope().track_builtin(FxtrID.builtin("typed-varpos-svc"))

            @service_method
            async def sum_all(self, *items: int) -> int:
                return sum(items)

        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_VarPosImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.sum_all() == 0
            assert await proxy.sum_all(1, 2, 3, 4) == 10

    async def test_var_keyword_only(self, service_layout: ServiceLayoutFactory) -> None:
        @service_protocol
        class _VarKwProto(Protocol):
            async def sum_flags(self, **flags: int) -> int: ...

        class _VarKwImpl(TypedServiceImpl[_VarKwProto]):
            def _target_type_annotation(self) -> Any:
                return _VarKwProto

            def _service_type_id(self) -> FxtrID:
                return get_active_scope().track_builtin(FxtrID.builtin("typed-varkw-svc"))

            @service_method
            async def sum_flags(self, **flags: int) -> int:
                return sum(flags.values())

        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_VarKwImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.sum_flags() == 0
            assert await proxy.sum_flags(a=1, b=2, c=3) == 6

    async def test_var_positional_and_var_keyword(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        # The same shape as the existing _VariadicImpl, but verifies the
        # successful round-trip path (existing tests only exercise the
        # rejection branch).
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_VariadicImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.collect() == 0
            assert await proxy.collect(1, 2, 3) == 6
            assert await proxy.collect(1, 2, x=10, y=20) == 33


# ---------------------------------------------------------------------------
# Nested-generics integration test
# ---------------------------------------------------------------------------
#
# Verifies that the type parameters of EntityRef / ServiceRef flow through
# correctly even when those refs are nested inside other collection types
# (here: ``list[tuple[EntityRef[_Container[E]], ServiceRef[_NestedInnerProto[E]]]]``)
# and the type parameter is *itself* a generic class. The proxy and impl
# both substitute the protocol's TypeVar for a concrete entity type, and
# the substitution must propagate through:
#
#   - the args at the impl side (concrete EntityRef / ServiceRef target_type);
#   - entities loaded inside the impl (EntityRef field of the loaded
#     entity carries the substituted type);
#   - service refs resolved inside the impl (the resolved service's own
#     return EntityRef carries the substituted type);
#   - the return values at the caller side (same propagation, swapped).


_LEAF_A_TYPE = system_entities.define_type(_TYPED_TYDEF_UUID, "nested-leaf-a")
_LEAF_B_TYPE = system_entities.define_type(_TYPED_TYDEF_UUID, "nested-leaf-b")
_CONTAINER_TYPE = system_entities.define_type(_TYPED_TYDEF_UUID, "nested-container")


@dataclasses.dataclass(frozen=True)
class _LeafA(DataclassEntity, fxtr_type=_LEAF_A_TYPE):
    val: int


@dataclasses.dataclass(frozen=True)
class _LeafB(DataclassEntity, fxtr_type=_LEAF_B_TYPE):
    text: str


@dataclasses.dataclass(frozen=True)
class _Container[E: Entity](DataclassEntity, fxtr_type=_CONTAINER_TYPE):
    """Generic parent entity holding a child of type ``E``."""

    label: str
    child: EntityRef[E]


_NESTED_INNER_TYPE_ID = FxtrID.builtin("typed-test-nested-inner-svc")
_NESTED_OUTER_TYPE_ID = FxtrID.builtin("typed-test-nested-outer-svc")


_E_NESTED_INNER = TypeVar("_E_NESTED_INNER", bound=Entity)
_E_NESTED_OUTER = TypeVar("_E_NESTED_OUTER", bound=Entity)


@service_protocol
class _NestedInnerProto(Protocol[_E_NESTED_INNER]):
    """Inner service generic on ``E``: takes a ``_Container[E]`` ref and
    returns the contained child as ``EntityRef[E]``."""

    async def child_of(
        self, container: EntityRef[_Container[_E_NESTED_INNER]]
    ) -> EntityRef[_E_NESTED_INNER]: ...


class _NestedInnerImpl[E: Entity](TypedServiceImpl[_NestedInnerProto[E]]):
    def __init__(self, type_arg: type[E]) -> None:
        self._type_arg = type_arg

    def _target_type_annotation(self) -> Any:
        return _NestedInnerProto[self._type_arg]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_NESTED_INNER_TYPE_ID)

    def _own_full_type_for_serialization(self) -> Any:
        return _NestedInnerImpl[self._type_arg]

    @service_method
    async def child_of(self, container: EntityRef[_Container[E]]) -> EntityRef[E]:
        loaded = await container.load()
        return loaded.child


@service_protocol
class _NestedOuterProto(Protocol[_E_NESTED_OUTER]):
    """Outer service generic on ``E``: takes a list of
    ``(container_ref, inner_svc_ref)`` pairs whose type parameters all
    resolve to ``E``, returns the same list with each pair's elements
    swapped."""

    async def swap(
        self,
        pairs: list[
            tuple[
                EntityRef[_Container[_E_NESTED_OUTER]],
                ServiceRef[_NestedInnerProto[_E_NESTED_OUTER]],
            ]
        ],
    ) -> list[
        tuple[
            ServiceRef[_NestedInnerProto[_E_NESTED_OUTER]],
            EntityRef[_Container[_E_NESTED_OUTER]],
        ]
    ]: ...


@dataclasses.dataclass
class _NestedObservedRow:
    """Snapshot of types observed inside ``swap`` for one input pair, so
    the test can assert against impl-side substitution without leaking
    refs across the scope boundary."""

    container_target: Any
    inner_target: Any
    child_target_via_load: Any
    child_target_via_svc: Any
    loaded_label: str


class _NestedOuterImpl[E: Entity](TypedServiceImpl[_NestedOuterProto[E]]):
    def __init__(self, type_arg: type[E]) -> None:
        self._type_arg = type_arg
        self.observed: list[_NestedObservedRow] = []

    def _target_type_annotation(self) -> Any:
        return _NestedOuterProto[self._type_arg]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_NESTED_OUTER_TYPE_ID)

    def _own_full_type_for_serialization(self) -> Any:
        return _NestedOuterImpl[self._type_arg]

    @service_method
    async def swap(
        self,
        pairs: list[tuple[EntityRef[_Container[E]], ServiceRef[_NestedInnerProto[E]]]],
    ) -> list[tuple[ServiceRef[_NestedInnerProto[E]], EntityRef[_Container[E]]]]:
        result: list[tuple[ServiceRef[_NestedInnerProto[E]], EntityRef[_Container[E]]]] = []
        for container_ref, inner_ref in pairs:
            loaded = await container_ref.load()
            inner_proxy = await inner_ref.resolve()
            child_via_svc = await inner_proxy.child_of(container_ref)
            self.observed.append(
                _NestedObservedRow(
                    container_target=container_ref.target_type,
                    inner_target=inner_ref.target_type,
                    child_target_via_load=loaded.child.target_type,
                    child_target_via_svc=child_via_svc.target_type,
                    loaded_label=loaded.label,
                )
            )
            result.append((inner_ref, container_ref))
        return result


class TestNestedGenericRefs:
    async def test_generic_typevar_propagates_through_nested_collections(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            inner_ref = await layout.service_scope.mount(_NestedInnerImpl(_LeafA))
            outer_impl = _NestedOuterImpl(_LeafA)
            outer_ref = await layout.service_scope.mount(outer_impl)

            visible_inner = await _make_ref_visible_from_caller(layout, inner_ref)
            visible_outer = await _make_ref_visible_from_caller(layout, outer_ref)

            outer_proxy = await visible_outer.resolve(layout.caller_scope)

            with layout.caller_scope.as_active_scope():
                # Build a (container_ref, inner_svc_ref) pair on the
                # caller side, with the container ref retyped to the
                # parameterized form so the wire annotation matches.
                leaf = _LeafA(val=42).store()
                container_ref = (
                    _Container(label="ctr-1", child=leaf)
                    .store()
                    .replace_target_type(_Container[_LeafA])
                )
                pairs_in: list[
                    tuple[
                        EntityRef[_Container[_LeafA]],
                        ServiceRef[_NestedInnerProto[_LeafA]],
                    ]
                ] = [(container_ref, visible_inner)]

                result = await outer_proxy.swap(pairs_in)

                # ---- Caller-side: returned refs propagate the concrete E. ----
                assert len(result) == 1
                got_inner, got_container = result[0]
                assert got_container.target_type == _Container[_LeafA]  # type: ignore[operator]
                assert got_inner.target_type == _NestedInnerProto[_LeafA]  # type: ignore[operator]

                # Loading the returned container yields a _Container[_LeafA]
                # whose child has target_type _LeafA — *not* the bound Entity
                # nor Any.
                loaded_back = await got_container.load()
                assert loaded_back.label == "ctr-1"
                assert loaded_back.child.target_type is _LeafA
                assert (await loaded_back.child.load()).val == 42

                # Resolving the returned inner ref and invoking it yields
                # an EntityRef whose target_type is the concretely
                # substituted _LeafA.
                inner_proxy = await got_inner.resolve(layout.caller_scope)
                child_via_inner = await inner_proxy.child_of(got_container)
                assert child_via_inner.target_type is _LeafA
                assert (await child_via_inner.load()).val == 42

            # ---- Impl-side: substitution propagated inside the call. ----
            assert len(outer_impl.observed) == 1
            obs = outer_impl.observed[0]
            assert obs.container_target == _Container[_LeafA]
            assert obs.inner_target == _NestedInnerProto[_LeafA]
            assert obs.child_target_via_load is _LeafA
            assert obs.child_target_via_svc is _LeafA
            assert obs.loaded_label == "ctr-1"


# ---------------------------------------------------------------------------
# FxtrServiceData annotation passthrough on Protocol / Impl signatures
# ---------------------------------------------------------------------------
#
# A parameter or return annotated ``FxtrServiceData`` opts out of
# dialect-level conversion. The value flows through unchanged; refs
# inside the value are not stripped to bare FxtrIDs.


@service_protocol
class _MixedSlotsProto(Protocol):
    """One typed slot (``EntityRef[T]``) plus one passthrough slot
    (``FxtrServiceData``); return type is also passthrough."""

    async def echo_with_ref(
        self, ref: EntityRef[_Note], payload: FxtrServiceData
    ) -> FxtrServiceData: ...


class _MixedSlotsImpl(TypedServiceImpl[_MixedSlotsProto]):
    def _target_type_annotation(self) -> Any:
        return _MixedSlotsProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-mixed-slots-svc"))

    @service_method
    async def echo_with_ref(
        self, ref: EntityRef[_Note], payload: FxtrServiceData
    ) -> FxtrServiceData:
        # Verify the typed slot got the strong type, and the passthrough
        # slot survived intact (including the inner EntityRef target_type).
        assert ref.target_type is _Note
        return {"ref_target_type_name": ref.target_type.__name__, "payload": payload}


@service_protocol
class _PassthroughVariadicProto(Protocol):
    """``*args: FxtrServiceData, **kwargs: FxtrServiceData`` — variadic
    elements pass through (each individually)."""

    async def collect(
        self, *items: FxtrServiceData, **flags: FxtrServiceData
    ) -> FxtrServiceData: ...


class _PassthroughVariadicImpl(TypedServiceImpl[_PassthroughVariadicProto]):
    def _target_type_annotation(self) -> Any:
        return _PassthroughVariadicProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-pt-variadic-svc"))

    @service_method
    async def collect(self, *items: FxtrServiceData, **flags: FxtrServiceData) -> FxtrServiceData:
        return {"items": list(items), "flags": dict(flags)}


@service_protocol
class _DefaultThenVariadicProto(Protocol):
    """A defaulted parameter FOLLOWED by a variadic — the shape where a
    call omitting both must not spill the (empty) variadic into an
    illegal keyword argument on the impl-side rebind (the decoded
    wrapper carries ``rest=()`` even when the wire omitted it, while the
    stripped ``mode`` leaves a gap ``BoundArguments`` can't bridge
    positionally)."""

    async def go(
        self, first: str, mode: str = "m-default", *rest: FxtrServiceData
    ) -> FxtrServiceData: ...


class _DefaultThenVariadicImpl(TypedServiceImpl[_DefaultThenVariadicProto]):
    def _target_type_annotation(self) -> Any:
        return _DefaultThenVariadicProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-def-variadic-svc"))

    @service_method
    async def go(
        self, first: str, mode: str = "m-default", *rest: FxtrServiceData
    ) -> FxtrServiceData:
        return {"first": first, "mode": mode, "rest": list(rest)}


@service_protocol
class _OptionalPassthroughProto(Protocol):
    """``payload: FxtrServiceData = USE_REMOTE_DEFAULT`` — optional
    passthrough slot. Verifies that the
    ``FxtrServiceData | _UseRemoteDefault`` union the wrapper synthesizes
    still flows through as expected (both members are passthrough strategies)."""

    async def maybe(self, payload: FxtrServiceData = USE_REMOTE_DEFAULT) -> FxtrServiceData: ...


class _OptionalPassthroughImpl(TypedServiceImpl[_OptionalPassthroughProto]):
    def _target_type_annotation(self) -> Any:
        return _OptionalPassthroughProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-opt-pt-svc"))

    @service_method
    async def maybe(self, payload: FxtrServiceData = "default-payload") -> FxtrServiceData:
        return payload


@service_protocol
class _DynamicConverterProto(Protocol):
    """Impl method takes ``payload: FxtrServiceData`` and dynamically
    decodes against a runtime-chosen annotation. This is the headline
    use case for the user-facing helpers — the typed Protocol lets the
    payload through untyped, and the impl decodes it to a strong type
    based on a discriminator the impl picks."""

    async def consume_typed_payload(
        self, kind: str, payload: FxtrServiceData
    ) -> FxtrServiceData: ...


class _DynamicConverterImpl(TypedServiceImpl[_DynamicConverterProto]):
    def _target_type_annotation(self) -> Any:
        return _DynamicConverterProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("typed-dyn-conv-svc"))

    @service_method
    async def consume_typed_payload(self, kind: str, payload: FxtrServiceData) -> FxtrServiceData:
        if kind == "note_ref":
            ref = decode_from_service_data(payload, EntityRef[_Note])
            assert ref.target_type is _Note
            loaded = await ref.load()
            return loaded.body
        if kind == "frozenset":
            xs = decode_from_service_data(payload, frozenset[int])
            return sorted(xs)
        raise AssertionError(f"unknown kind: {kind}")


class TestFxtrServiceDataPassthroughOnMethods:
    async def test_mixed_typed_and_passthrough_slots(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_MixedSlotsImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            with layout.caller_scope.as_active_scope():
                note_ref = _Note(body="hello").store()
                # The payload contains a primitive payload; the typed
                # slot carries the EntityRef. The result is passthrough
                # so it comes back as-is.
                result = await proxy.echo_with_ref(note_ref, {"a": 1, "b": "two"})
                assert result == {
                    "ref_target_type_name": "_Note",
                    "payload": {"a": 1, "b": "two"},
                }

    async def test_variadic_passthrough_each_element(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_PassthroughVariadicImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            result = await proxy.collect(1, "two", [3, 4], a={"nested": True}, b=5)
            assert result == {
                "items": [1, "two", [3, 4]],
                "flags": {"a": {"nested": True}, "b": 5},
            }

    async def test_default_then_variadic_omitting_both(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        # ``go("x")`` omits the defaulted ``mode`` AND the variadic
        # ``rest``: the impl-side rebind must apply the impl's default
        # and call with no variadic — not pass ``rest=()`` as a keyword
        # (the empty-variadic-after-a-gap regression).
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_DefaultThenVariadicImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            result = await proxy.go("x")
            assert result == {"first": "x", "mode": "m-default", "rest": []}
            # Supplying everything still routes the variadic positionally.
            result = await proxy.go("x", "custom", 1, "two")
            assert result == {"first": "x", "mode": "custom", "rest": [1, "two"]}

    async def test_optional_passthrough_use_remote_default(
        self, service_layout: ServiceLayoutFactory
    ) -> None:
        # Protocol declares ``payload: FxtrServiceData = USE_REMOTE_DEFAULT``.
        # Wrapper field type becomes ``FxtrServiceData | _UseRemoteDefault``
        # — both passthrough strategies. The proxy strips the sentinel,
        # the impl applies its own default.
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_OptionalPassthroughImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            assert await proxy.maybe() == "default-payload"
            assert await proxy.maybe(payload={"nested": [1, 2, 3]}) == {"nested": [1, 2, 3]}

    async def test_dynamic_decode_inside_impl(self, service_layout: ServiceLayoutFactory) -> None:
        # Caller hands an opaque-looking ``FxtrServiceData`` payload
        # over the wire; the impl decodes it against a runtime-chosen
        # annotation using ``decode_from_service_data``. Mirror with
        # ``encode_to_service_data`` on the caller side.
        async with service_layout() as layout:
            ref = await layout.service_scope.mount(_DynamicConverterImpl())
            visible = await _make_ref_visible_from_caller(layout, ref)
            proxy = await visible.resolve(layout.caller_scope)
            with layout.caller_scope.as_active_scope():
                note_ref = _Note(body="dyn-hello").store()
                payload = encode_to_service_data(note_ref, EntityRef[_Note])
                got = await proxy.consume_typed_payload("note_ref", payload)
                assert got == "dyn-hello"

                # frozenset variant — purely scalar payload, exercises
                # the helpers' dialect-driven conversion of a non-ref
                # vocabulary type.
                xs_payload = encode_to_service_data(frozenset({3, 1, 2}), frozenset[int])
                got_xs = await proxy.consume_typed_payload("frozenset", xs_payload)
                assert got_xs == [1, 2, 3]
