"""Tests for genuine ``@service_protocol`` subclassing.

Covers the part-0 change-set of stage 13
(``plans/phase3/stage13_output_channels/part0_protocol_subclassing.md``):

- the ``allow_protocol_subtypes`` opt-in flag (most-specific resolver
  wins; unmarked bases still conflict at resolve; undecorated
  subclasses fall back to the base's resolver),
- TypeVar substitution for inherited methods (fully-bound,
  still-generic, multi-hop, unparameterized-base fallback, same-name
  TypeVar collision, override-wins),
- end-to-end mount/resolve round-trips through inherited non-generic
  and generic methods (proving the impl receives real decoded
  instances, not bound-fallback wire dicts),
- the relaxed ``EventSource`` / ``Subscriber`` bounds enabling the
  stage-13 `EventSource`-subclass shape.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    _SERVICE_RESOLVER_REGISTRY,
    GLOBAL_SCOPE,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.services.subscription_types import EventSource, Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import TypedServiceImpl, _typevar_subst_map, service_protocol
from fxtr2.services.untyped_service import service_method

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Payload + protocol hierarchy under test
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Payload:
    """Plain dataclass payload — *not* an Entity, so receiving a real
    instance on the impl side proves the typed codec ran with the
    substituted annotation (an ``Any``-typed bound-fallback would hand
    the impl the raw wire dict instead)."""

    label: str
    n: int


@service_protocol(allow_protocol_subtypes=True)
class EmitterBase[T](Protocol):
    """Marked base: one generic method, one non-generic method."""

    async def emit(self, item: T) -> T: ...

    async def ping(self) -> str: ...


@service_protocol
class PayloadEmitter(EmitterBase[_Payload], Protocol):
    """Fully-bound subclass adding its own method."""

    async def extra(self) -> int: ...


@service_protocol(allow_protocol_subtypes=True)
class GenericEmitter[V](EmitterBase[V], Protocol):
    """Still-generic subclass (and itself a marked base for multi-hop)."""


@service_protocol
class MultiHopEmitter(GenericEmitter[_Payload], Protocol):
    """Two-hop chain: EmitterBase[T] -> GenericEmitter[V] -> bound."""


@service_protocol
class UnparamEmitter(EmitterBase, Protocol):
    """Unparameterized base: T stays unsubstituted (bound fallback)."""


@service_protocol
class SameNameTV[T](EmitterBase[T], Protocol):
    """Reuses the base's TypeVar *name* — substitution must key on
    TypeVar objects, not names."""


@service_protocol
class RedeclaredEmitter(EmitterBase[_Payload], Protocol):
    """Re-declares an inherited method purely for the docstring —
    override-wins, same wire shape."""

    async def emit(self, item: _Payload) -> _Payload:
        """A richer docstring."""
        ...


class UndecoratedEmitter(EmitterBase[_Payload], Protocol):
    """No ``@service_protocol`` — resolves via the base's resolver."""


@service_protocol
class UnmarkedBase(Protocol):
    """A base *without* ``allow_protocol_subtypes`` — subclass
    registration succeeds but subclass resolve must conflict."""

    async def nop(self) -> None: ...


@service_protocol
class SubOfUnmarked(UnmarkedBase, Protocol):
    pass


# ---------------------------------------------------------------------------
# Impls
# ---------------------------------------------------------------------------


class PayloadEmitterImpl(TypedServiceImpl[PayloadEmitter]):
    def _target_type_annotation(self) -> Any:
        return PayloadEmitter

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("proto-subclass-payload-emitter"))

    @service_method
    async def emit(self, item: _Payload) -> _Payload:
        # The isinstance check is the point of the test: bound-fallback
        # would deliver the wire dict here.
        assert isinstance(item, _Payload), f"impl got {type(item).__name__}, not _Payload"
        return dataclasses.replace(item, n=item.n + 1)

    @service_method
    async def ping(self) -> str:
        return "pong"

    @service_method
    async def extra(self) -> int:
        return 7


class MultiHopEmitterImpl(TypedServiceImpl[MultiHopEmitter]):
    def _target_type_annotation(self) -> Any:
        return MultiHopEmitter

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("proto-subclass-multihop-emitter"))

    @service_method
    async def emit(self, item: _Payload) -> _Payload:
        assert isinstance(item, _Payload)
        return dataclasses.replace(item, label=item.label + "!")

    @service_method
    async def ping(self) -> str:
        return "multihop-pong"


# ---------------------------------------------------------------------------
# Registry behavior
# ---------------------------------------------------------------------------


class TestResolverRegistry:
    def test_marked_base_subclass_resolves_with_own_resolver(self) -> None:
        matched = _SERVICE_RESOLVER_REGISTRY.find_handler(PayloadEmitter)
        assert matched.entry.key is PayloadEmitter

    def test_base_type_lookup_unaffected(self) -> None:
        matched = _SERVICE_RESOLVER_REGISTRY.find_handler(EmitterBase[_Payload])
        assert matched.entry.key is EmitterBase
        matched_bare = _SERVICE_RESOLVER_REGISTRY.find_handler(EmitterBase)
        assert matched_bare.entry.key is EmitterBase

    def test_unmarked_base_subclass_conflicts_at_resolve(self) -> None:
        # Registration above succeeded; the guard fires at lookup.
        with pytest.raises(ValueError, match="Conflicting handlers"):
            _SERVICE_RESOLVER_REGISTRY.find_handler(SubOfUnmarked)
        # The unmarked base itself still resolves fine.
        assert _SERVICE_RESOLVER_REGISTRY.find_handler(UnmarkedBase).entry.key is UnmarkedBase

    def test_undecorated_subclass_falls_back_to_base_resolver(self) -> None:
        matched = _SERVICE_RESOLVER_REGISTRY.find_handler(UndecoratedEmitter)
        assert matched.entry.key is EmitterBase

    def test_subclass_metadata_includes_inherited_methods(self) -> None:
        meta = PayloadEmitter._fxtr_service_protocol_metadata  # type: ignore[attr-defined]
        assert set(meta.method_specs.keys()) == {"emit", "ping", "extra"}
        # Undecorated subclass *inherits* the base's metadata object
        # (plain class attribute) — pin the documented behavior.
        base_meta = EmitterBase._fxtr_service_protocol_metadata  # type: ignore[attr-defined]
        inherited = UndecoratedEmitter._fxtr_service_protocol_metadata  # type: ignore[attr-defined]
        assert inherited is base_meta


# ---------------------------------------------------------------------------
# TypeVar substitution (unit level: subst maps + wrapper field types)
# ---------------------------------------------------------------------------


def _args_field_type(proto: type, method: str, field: str) -> Any:
    meta = proto._fxtr_service_protocol_metadata  # type: ignore[attr-defined]
    spec = meta.method_specs[method]
    types_by_name = {f.name: f.type for f in dataclasses.fields(spec.args_wrapper_cls)}
    return types_by_name[field]


def _sole_type_param(cls: type) -> Any:
    """The class's single class-level TypeVar (via ``__parameters__``)."""
    (param,) = getattr(cls, "__parameters__")
    return param


class TestTypeVarSubstitution:
    def test_fully_bound_single_hop(self) -> None:
        base_t = _sole_type_param(EmitterBase)
        assert _typevar_subst_map(PayloadEmitter, EmitterBase) == {base_t: _Payload}
        assert _args_field_type(PayloadEmitter, "emit", "item") is _Payload

    def test_still_generic_subclass(self) -> None:
        base_t = _sole_type_param(EmitterBase)
        sub_v = _sole_type_param(GenericEmitter)
        assert _typevar_subst_map(GenericEmitter, EmitterBase) == {base_t: sub_v}
        # The wrapper field carries the *subclass's* TypeVar, so a
        # parameterized resolve (GenericEmitter[X]) substitutes it.
        assert _args_field_type(GenericEmitter, "emit", "item") is sub_v

    def test_multi_hop_composes(self) -> None:
        base_t = _sole_type_param(EmitterBase)
        assert _typevar_subst_map(MultiHopEmitter, EmitterBase) == {base_t: _Payload}
        assert _args_field_type(MultiHopEmitter, "emit", "item") is _Payload

    def test_unparameterized_base_falls_back(self) -> None:
        base_t = _sole_type_param(EmitterBase)
        assert _typevar_subst_map(UnparamEmitter, EmitterBase) == {}
        # The field still references the base's TypeVar — codec build
        # resolves it to its bound (the pre-part-0 behavior, now
        # explicit and documented).
        assert _args_field_type(UnparamEmitter, "emit", "item") is base_t

    def test_same_name_typevar_keys_on_objects(self) -> None:
        base_t = _sole_type_param(EmitterBase)
        sub_t = _sole_type_param(SameNameTV)
        assert base_t.__name__ == sub_t.__name__ == "T"
        assert base_t is not sub_t
        assert _typevar_subst_map(SameNameTV, EmitterBase) == {base_t: sub_t}
        assert _args_field_type(SameNameTV, "emit", "item") is sub_t

    def test_override_wins(self) -> None:
        meta = RedeclaredEmitter._fxtr_service_protocol_metadata  # type: ignore[attr-defined]
        spec = meta.method_specs["emit"]
        # The re-declaration's own annotations drive the wrapper (and
        # its docstring is preserved on the walked function).
        assert _args_field_type(RedeclaredEmitter, "emit", "item") is _Payload
        assert "richer docstring" in (spec.signature and RedeclaredEmitter.emit.__doc__ or "")

    def test_eventsource_subclass_shape(self) -> None:
        # The stage-13 shape: a subclass binding EventSource's payload
        # type gets the substituted typed subscribe param.
        @service_protocol
        class _ChannelLike(EventSource[_Payload], Protocol):
            async def status(self) -> str: ...

        anno = _args_field_type(_ChannelLike, "subscribe", "subscriber")
        assert anno == ServiceRef[Subscriber[_Payload]]
        matched = _SERVICE_RESOLVER_REGISTRY.find_handler(_ChannelLike)
        assert matched.entry.key is _ChannelLike


# ---------------------------------------------------------------------------
# End-to-end mount/resolve round-trips
# ---------------------------------------------------------------------------


class TestEndToEnd:
    async def test_inherited_methods_round_trip(self) -> None:
        async with FxtrScope(name="sub-e2e", include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(PayloadEmitterImpl())
            proxy = await ref.resolve(scope)
            # Inherited non-generic method (the NixDispatcherProto shape).
            assert await proxy.ping() == "pong"
            # Own method declared on the subclass.
            assert await proxy.extra() == 7
            # Inherited generic method with the subclass's binding: the
            # impl asserts it receives a real _Payload (decode), and the
            # caller gets a real _Payload back (encode + return decode).
            result = await proxy.emit(_Payload(label="x", n=1))
            assert result == _Payload(label="x", n=2)

    async def test_multi_hop_round_trip(self) -> None:
        async with FxtrScope(name="hop-e2e", include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(MultiHopEmitterImpl())
            proxy = await ref.resolve(scope)
            assert await proxy.ping() == "multihop-pong"
            result = await proxy.emit(_Payload(label="y", n=0))
            assert result == _Payload(label="y!", n=0)

    async def test_retype_to_base_still_dispatches(self) -> None:
        # Wire dispatch is by method name: a ref re-typed to the base
        # resolves the base's proxy and reaches the same impl.
        async with FxtrScope(name="retype-e2e", include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(PayloadEmitterImpl())
            base_ref = ref.replace_service_type(EmitterBase[_Payload])
            base_proxy = await base_ref.resolve(scope)
            assert await base_proxy.ping() == "pong"
            result = await base_proxy.emit(_Payload(label="z", n=10))
            assert result == _Payload(label="z", n=11)

    async def test_undecorated_subclass_resolves_with_base_vocabulary(self) -> None:
        async with FxtrScope(name="undec-e2e", include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(PayloadEmitterImpl())
            undec_ref = ref.replace_service_type(UndecoratedEmitter)
            proxy = await undec_ref.resolve(scope)
            # Base vocabulary: inherited methods exist...
            assert await proxy.ping() == "pong"
            # ...the subclass's own additions don't.
            assert not hasattr(proxy, "extra")

    async def test_undecorated_subclass_keeps_typevar_binding(self) -> None:
        # An *undecorated* subclass that binds the base's TypeVars
        # (UndecoratedEmitter binds T=_Payload via its base list)
        # resolves with the binding honored: the proxy recovers the
        # parameterization from the inheritance chain
        # (_proxy_parameterization), so typed codecs run exactly as if
        # the ref were annotated ``EmitterBase[_Payload]``.
        async with FxtrScope(name="undec-bind", include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(PayloadEmitterImpl())
            proxy = await ref.replace_service_type(UndecoratedEmitter).resolve(scope)
            result = await proxy.emit(_Payload(label="x", n=1))
            assert result == _Payload(label="x", n=2)

    async def test_undecorated_generic_subclass_parameterized_at_use_site(self) -> None:
        # An undecorated *still-generic* subclass parameterized at the
        # use site: the use-site args bind the subclass's own TypeVar,
        # which composes through the inheritance map down to the
        # base's TypeVar.
        class UndecGeneric[W](EmitterBase[W], Protocol):
            pass

        async with FxtrScope(name="undec-gen", include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(PayloadEmitterImpl())
            proxy = await ref.replace_service_type(UndecGeneric[_Payload]).resolve(scope)
            assert await proxy.ping() == "pong"
            result = await proxy.emit(_Payload(label="y", n=5))
            assert result == _Payload(label="y", n=6)


# ---------------------------------------------------------------------------
# Generic *impl* base classes (the TypedServiceImpl side of change 2)
# ---------------------------------------------------------------------------


class _GenericEmitterImplBase[T](TypedServiceImpl[EmitterBase[T]]):
    """Generic impl base with tagged methods; concrete subclasses bind T."""

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("proto-subclass-impl-base"))

    @service_method
    async def emit(self, item: T) -> T:
        assert isinstance(item, _Payload)
        return item

    @service_method
    async def ping(self) -> str:
        return "impl-base-pong"


class _BoundEmitterImpl(_GenericEmitterImplBase[_Payload]):
    """Concrete impl inheriting tagged methods from a generic base."""

    def _target_type_annotation(self) -> Any:
        return EmitterBase[_Payload]


class TestInheritedImplMethods:
    async def test_impl_inherits_tagged_methods_with_substitution(self) -> None:
        async with FxtrScope(name="impl-e2e", include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(_BoundEmitterImpl())
            proxy = await ref.resolve(scope)
            assert await proxy.ping() == "impl-base-pong"
            result = await proxy.emit(_Payload(label="impl", n=3))
            assert result == _Payload(label="impl", n=3)


# ---------------------------------------------------------------------------
# Relaxed Subscriber/EventSource bounds
# ---------------------------------------------------------------------------


class TestRelaxedBounds:
    def test_unbounded_payload_type_accepted(self) -> None:
        # _Payload is not FxtrServiceData; under the old bound this
        # parameterization was rejected by typecheckers and resolved to
        # the bound at codec build. Now it's a first-class binding.
        anno = _args_field_type(GenericEmitter, "emit", "item")
        assert anno is _sole_type_param(GenericEmitter)
        # EventSource/Subscriber remain resolvable as plain bases.
        assert _SERVICE_RESOLVER_REGISTRY.find_handler(EventSource).entry.key is EventSource
        assert _SERVICE_RESOLVER_REGISTRY.find_handler(Subscriber).entry.key is Subscriber
        assert (
            _SERVICE_RESOLVER_REGISTRY.find_handler(UnsubscribeHandle).entry.key
            is UnsubscribeHandle
        )
