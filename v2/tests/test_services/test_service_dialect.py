"""Focused tests for ``FxtrServicesDefaultDialect`` strategies.

Each strategy is exercised through a tiny dynamically-built
wrapper dataclass that mirrors the wrapper-dataclass pattern
substage 8 uses for typed services. The wrapper dataclass is a
plain ``@dataclass`` (NOT ``DataClassDictMixin``) so the dialect
threads through at codec-build time — see ``part8_plan.md``
"Spike notes" for why.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from typing import Any, Generic, TypeVar

import pytest
from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, ServiceRef
from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.leased_cap_ir import IRHandle, IRValue, Request, UnwrapForPipelining
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.service_dialect import (
    OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE,
    EntityRefServiceStrategy,
    FrozendictServiceStrategy,
    FrozensetServiceStrategy,
    FxtrServicesDefaultDialect,
    NotFxtrServiceDataError,
    OverrideThisTypeBeforeUsing,
    ServiceRefServiceStrategy,
    SetServiceStrategy,
    decode_from_service_data,
    encode_to_service_data,
    get_basic_decoder,
    get_basic_encoder,
)

# Module-level entity types used as ``T`` in EntityRef[T].
_DIALECT_TYDEF_UUID = uuid.UUID("c1e0f8a4-7b2d-4a3a-9c5e-1d2c3b4a5e6f")


@dataclasses.dataclass(frozen=True)
class _Foo(DataclassEntity, fxtr_type=system_entities.define_type(_DIALECT_TYDEF_UUID, "foo")):
    body: str


@dataclasses.dataclass(frozen=True)
class _Bar(DataclassEntity, fxtr_type=system_entities.define_type(_DIALECT_TYDEF_UUID, "bar")):
    body: str


# A throwaway protocol-like target type for ServiceRef[T].
class _SomeProto:
    pass


class _OtherProto:
    pass


# ---------------------------------------------------------------------------
# Helpers — build a parameterized wrapper dataclass for codec testing.
# ---------------------------------------------------------------------------

T = TypeVar("T")


def _build_wrapper(
    name: str, fields: list[tuple[str, Any]], type_params: tuple[TypeVar, ...] = (T,)
) -> type:
    """Make a plain ``@dataclass`` parameterized by ``type_params``.

    Mirrors the wrapper-dataclass build pattern from the §4 plan
    (see ``part8_spike.py`` for the validation that this works).
    """
    return dataclasses.make_dataclass(
        name,
        fields=fields,
        bases=(Generic[*type_params],),  # type: ignore[valid-type]
        frozen=True,
    )


def _roundtrip(wrapper_cls: Any, instance: Any) -> Any:
    enc = get_basic_encoder(wrapper_cls, FxtrServicesDefaultDialect)
    dec = get_basic_decoder(wrapper_cls, FxtrServicesDefaultDialect)
    return dec.decode(enc.encode(instance))


# ---------------------------------------------------------------------------
# EntityRef[T] strategy
# ---------------------------------------------------------------------------


class TestEntityRefStrategy:
    def test_encode_strips_target_type_to_bare_fxtr_id(self) -> None:
        Wrapper = _build_wrapper("W_ER_enc", [("ref", EntityRef[T])])  # type: ignore[valid-type]
        fid = FxtrID.builtin("er-enc-test")
        enc = get_basic_encoder(Wrapper[_Foo], FxtrServicesDefaultDialect)  # type: ignore[index]
        encoded = enc.encode(Wrapper(ref=EntityRef(fid, _Foo)))  # type: ignore[call-arg]
        assert encoded == {"ref": fid}

    def test_decode_re_attaches_target_type(self) -> None:
        Wrapper = _build_wrapper("W_ER_dec", [("ref", EntityRef[T])])  # type: ignore[valid-type]
        fid = FxtrID.builtin("er-dec-test")
        decoded = _roundtrip(Wrapper[_Foo], Wrapper(ref=EntityRef(fid, _Foo)))  # type: ignore[index, call-arg]
        assert isinstance(decoded.ref, EntityRef)
        assert decoded.ref.entity_id == fid
        assert decoded.ref.target_type is _Foo

    def test_decode_t_substitution_is_per_parameterization(self) -> None:
        # Same wrapper class, two different parameterizations → different T.
        Wrapper = _build_wrapper("W_ER_diff", [("ref", EntityRef[T])])  # type: ignore[valid-type]
        fid = FxtrID.builtin("er-diff-test")
        d_foo = _roundtrip(Wrapper[_Foo], Wrapper(ref=EntityRef(fid, _Foo)))  # type: ignore[index, call-arg]
        d_bar = _roundtrip(Wrapper[_Bar], Wrapper(ref=EntityRef(fid, _Bar)))  # type: ignore[index, call-arg]
        assert d_foo.ref.target_type is _Foo
        assert d_bar.ref.target_type is _Bar

    def test_encode_rejects_non_entityref(self) -> None:
        Wrapper = _build_wrapper("W_ER_bad", [("ref", EntityRef[T])])  # type: ignore[valid-type]
        enc = get_basic_encoder(Wrapper[_Foo], FxtrServicesDefaultDialect)  # type: ignore[index]
        with pytest.raises(Exception):
            # Note: we go through the strategy which receives the wrong type;
            # encode() may surface as TypeError or as InvalidFieldValue.
            enc.encode(Wrapper(ref="not-a-ref"))  # type: ignore[arg-type, call-arg]

    def test_decode_rejects_entityref_input(self) -> None:
        # Decoder expects a bare FxtrID at the dialect layer; an EntityRef
        # in the wire form is a contract violation.
        Wrapper = _build_wrapper("W_ER_bad_dec", [("ref", EntityRef[T])])  # type: ignore[valid-type]
        dec = get_basic_decoder(Wrapper[_Foo], FxtrServicesDefaultDialect)  # type: ignore[index]
        fid = FxtrID.builtin("er-bad-dec")
        with pytest.raises(Exception):
            dec.decode({"ref": EntityRef(fid, _Foo)})


# ---------------------------------------------------------------------------
# ServiceRef[T] strategy
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_handle() -> Any:
    """Build an inert IRHandle backed by a temporary ULL.

    The strategies only inspect ``ref.handle`` identity; we don't need
    a working dispatch path. The ULL is revoked on teardown so the
    autouse leak fixture stays happy.
    """
    from fxtr2.leases.leased_cap_ir import IRHandle

    ull = UnconditionalLocalLifetime("test-svc-ref")
    # Discard the root lease — we don't need it; the only thing keeping
    # the ULL alive after this is the IRHandle's token reference.
    ull.root_lease.discard()

    class _StubHandle(IRHandle[Any]):  # type: ignore[misc]
        @property
        def token(self) -> Any:
            return ull.token

        def dispatch(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
            raise NotImplementedError

        def request(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
            raise NotImplementedError

        def discard(self) -> None:
            pass

    yield _StubHandle()


def _make_service_ref(handle: Any, target_type: Any = UnknownService) -> ServiceRef[Any]:
    """Build a ServiceRef bound to ``handle``.

    The strategies only inspect ``ref.handle`` / ``ref.service_type_id`` /
    ``ref.target_type``; we don't need a working IRHandle to test
    encode/decode mechanics.
    """
    return ServiceRef(
        handle=handle,
        target_type=target_type,
        service_type_id=FxtrID.builtin("test-svc-type"),
    )


class TestServiceRefStrategy:
    def test_encode_strips_to_unknown_service(self, stub_handle: Any) -> None:
        Wrapper = _build_wrapper("W_SR_enc", [("ref", ServiceRef[T])])  # type: ignore[valid-type]
        ref = _make_service_ref(stub_handle, target_type=_SomeProto)
        enc = get_basic_encoder(Wrapper[_SomeProto], FxtrServicesDefaultDialect)  # type: ignore[index]
        encoded = enc.encode(Wrapper(ref=ref))  # type: ignore[call-arg]
        wire_ref = encoded["ref"]
        assert isinstance(wire_ref, ServiceRef)
        assert wire_ref.target_type is UnknownService
        assert wire_ref.handle is ref.handle
        assert wire_ref.service_type_id == ref.service_type_id

    def test_decode_re_attaches_target_type(self, stub_handle: Any) -> None:
        Wrapper = _build_wrapper("W_SR_dec", [("ref", ServiceRef[T])])  # type: ignore[valid-type]
        ref = _make_service_ref(stub_handle, target_type=UnknownService)
        decoded = _roundtrip(Wrapper[_SomeProto], Wrapper(ref=ref))  # type: ignore[index, call-arg]
        assert isinstance(decoded.ref, ServiceRef)
        assert decoded.ref.target_type is _SomeProto
        assert decoded.ref.service_type_id == ref.service_type_id

    def test_decode_t_substitution_is_per_parameterization(self, stub_handle: Any) -> None:
        Wrapper = _build_wrapper("W_SR_diff", [("ref", ServiceRef[T])])  # type: ignore[valid-type]
        ref = _make_service_ref(stub_handle, target_type=UnknownService)
        d_some = _roundtrip(Wrapper[_SomeProto], Wrapper(ref=ref))  # type: ignore[index, call-arg]
        d_other = _roundtrip(Wrapper[_OtherProto], Wrapper(ref=ref))  # type: ignore[index, call-arg]
        assert d_some.ref.target_type is _SomeProto
        assert d_other.ref.target_type is _OtherProto

    def test_decode_rejects_typed_input(self, stub_handle: Any) -> None:
        # Decoder expects ServiceRef[UnknownService]; a typed ref on the
        # wire is a contract violation.
        Wrapper = _build_wrapper("W_SR_bad", [("ref", ServiceRef[T])])  # type: ignore[valid-type]
        dec = get_basic_decoder(Wrapper[_SomeProto], FxtrServicesDefaultDialect)  # type: ignore[index]
        typed_ref = _make_service_ref(stub_handle, target_type=_SomeProto)
        with pytest.raises(Exception):
            dec.decode({"ref": typed_ref})


# ---------------------------------------------------------------------------
# Collection strategies
# ---------------------------------------------------------------------------


class TestCollectionStrategies:
    def test_frozenset_roundtrip(self) -> None:
        Wrapper = _build_wrapper("W_FS", [("xs", frozenset[int])])
        encoded = get_basic_encoder(Wrapper, FxtrServicesDefaultDialect).encode(
            Wrapper(xs=frozenset({1, 2, 3}))  # type: ignore[call-arg]
        )
        assert sorted(encoded["xs"]) == [1, 2, 3]
        decoded = _roundtrip(Wrapper, Wrapper(xs=frozenset({1, 2, 3})))  # type: ignore[call-arg]
        assert decoded.xs == frozenset({1, 2, 3})

    def test_set_roundtrip(self) -> None:
        Wrapper = _build_wrapper("W_SET", [("xs", set[int])])
        decoded = _roundtrip(Wrapper, Wrapper(xs={4, 5, 6}))  # type: ignore[call-arg]
        assert decoded.xs == {4, 5, 6}

    def test_frozendict_roundtrip(self) -> None:
        Wrapper = _build_wrapper("W_FD", [("d", frozendict[str, int])])  # type: ignore[type-arg]
        decoded = _roundtrip(Wrapper, Wrapper(d=frozendict({"a": 1, "b": 2})))  # type: ignore[call-arg]
        assert decoded.d == frozendict({"a": 1, "b": 2})

    def test_tuple_fixed_roundtrip(self) -> None:
        Wrapper = _build_wrapper("W_T_FIXED", [("t", tuple[int, str])])
        decoded = _roundtrip(Wrapper, Wrapper(t=(1, "two")))  # type: ignore[call-arg]
        assert decoded.t == (1, "two")

    def test_tuple_variadic_roundtrip(self) -> None:
        Wrapper = _build_wrapper("W_T_VAR", [("t", tuple[int, ...])])
        decoded = _roundtrip(Wrapper, Wrapper(t=(1, 2, 3, 4)))  # type: ignore[call-arg]
        assert decoded.t == (1, 2, 3, 4)


# ---------------------------------------------------------------------------
# Nested vocabulary types under collections / unions
# ---------------------------------------------------------------------------


class TestNestedVocabulary:
    def test_list_of_entityrefs_substitutes_t(self) -> None:
        Wrapper = _build_wrapper("W_LIST_ER", [("refs", list[EntityRef[T]])])  # type: ignore[valid-type]
        fid_a = FxtrID.builtin("nested-a")
        fid_b = FxtrID.builtin("nested-b")
        decoded = _roundtrip(
            Wrapper[_Foo],  # type: ignore[index]
            Wrapper(refs=[EntityRef(fid_a, _Foo), EntityRef(fid_b, _Foo)]),  # type: ignore[call-arg]
        )
        assert all(r.target_type is _Foo for r in decoded.refs)
        assert [r.entity_id for r in decoded.refs] == [fid_a, fid_b]

    def test_optional_entityref(self) -> None:
        Wrapper = _build_wrapper("W_OPT_ER", [("ref", EntityRef[T] | None)])  # type: ignore[valid-type]
        fid = FxtrID.builtin("opt-er")
        decoded_some = _roundtrip(Wrapper[_Foo], Wrapper(ref=EntityRef(fid, _Foo)))  # type: ignore[index, call-arg]
        assert decoded_some.ref is not None
        assert decoded_some.ref.target_type is _Foo
        decoded_none = _roundtrip(Wrapper[_Foo], Wrapper(ref=None))  # type: ignore[index, call-arg]
        assert decoded_none.ref is None

    def test_dict_of_serviceref_values(self, stub_handle: Any) -> None:
        Wrapper = _build_wrapper("W_DICT_SR", [("d", dict[str, ServiceRef[T]])])  # type: ignore[valid-type]
        ref = _make_service_ref(stub_handle, target_type=UnknownService)
        decoded = _roundtrip(Wrapper[_SomeProto], Wrapper(d={"k": ref}))  # type: ignore[index, call-arg]
        assert decoded.d["k"].target_type is _SomeProto


# ---------------------------------------------------------------------------
# Codec cache
# ---------------------------------------------------------------------------


class TestCodecCache:
    def test_get_basic_encoder_cached(self) -> None:
        Wrapper = _build_wrapper("W_CACHE", [("x", int)])
        e1 = get_basic_encoder(Wrapper, FxtrServicesDefaultDialect)
        e2 = get_basic_encoder(Wrapper, FxtrServicesDefaultDialect)
        assert e1 is e2

    def test_get_basic_decoder_cached(self) -> None:
        Wrapper = _build_wrapper("W_CACHE_DEC", [("x", int)])
        d1 = get_basic_decoder(Wrapper, FxtrServicesDefaultDialect)
        d2 = get_basic_decoder(Wrapper, FxtrServicesDefaultDialect)
        assert d1 is d2


# ---------------------------------------------------------------------------
# Dialect extension via merge
# ---------------------------------------------------------------------------


class TestDialectExtension:
    def test_subclass_dialect_can_add_strategy(self) -> None:
        # Custom user type with its own strategy.
        @dataclasses.dataclass(frozen=True)
        class CustomBox:
            n: int

        class _BoxStrategy(mashumaro_types_strategy()):  # type: ignore[misc]
            def serialize(self, value: CustomBox) -> int:
                return value.n

            def deserialize(self, value: int) -> CustomBox:
                return CustomBox(n=value)

        class CustomDialect(FxtrServicesDefaultDialect):
            serialization_strategy = {
                **FxtrServicesDefaultDialect.serialization_strategy,
                CustomBox: _BoxStrategy(),
            }

        Wrapper = _build_wrapper("W_CUSTOM", [("box", CustomBox), ("xs", frozenset[int])])
        decoded = _roundtrip_with_dialect(
            Wrapper, Wrapper(box=CustomBox(7), xs=frozenset({1, 2})), CustomDialect  # type: ignore[call-arg]
        )
        assert decoded.box == CustomBox(7)
        assert decoded.xs == frozenset({1, 2})


def _roundtrip_with_dialect(wrapper_cls: Any, instance: Any, dialect: Any) -> Any:
    enc = get_basic_encoder(wrapper_cls, dialect)
    dec = get_basic_decoder(wrapper_cls, dialect)
    return dec.decode(enc.encode(instance))


def mashumaro_types_strategy() -> Any:
    """Late import for the test that uses a custom strategy."""
    import mashumaro.types

    return mashumaro.types.SerializationStrategy


# ---------------------------------------------------------------------------
# FxtrServiceData passthrough strategy
# ---------------------------------------------------------------------------


class TestFxtrServiceDataPassthrough:
    """The dialect registers ``FxtrServiceData`` as a passthrough strategy.

    The lookup hits before mashumaro unwraps the ``TypeAliasType`` to
    its ``__value__`` (which would otherwise expand the union and apply
    the typed strategies — wrong for an opt-out slot). These tests pin
    that contract: a future mashumaro version that reorders the
    dispatch chain should fail loudly here.
    """

    def test_top_level_field_passes_through_primitive(self) -> None:
        Wrapper = _build_wrapper("W_FSD_PRIM", [("x", FxtrServiceData)])  # type: ignore[valid-type]
        # Encode/decode a value that looks vocabulary-shaped but contains
        # an EntityRef. With pass_through, the EntityRef is NOT stripped
        # to a bare FxtrID — it survives both directions intact.
        fid = FxtrID.builtin("psd-prim")
        ref = EntityRef(fid, _Foo)
        encoded = get_basic_encoder(Wrapper, FxtrServicesDefaultDialect).encode(
            Wrapper(x=ref)  # type: ignore[call-arg]
        )
        # No conversion: the EntityRef passes through.
        assert encoded["x"] is ref
        decoded = _roundtrip(Wrapper, Wrapper(x=ref))  # type: ignore[call-arg]
        assert decoded.x is ref

    def test_nested_in_tuple_passes_through(self) -> None:
        # ``tuple[FxtrServiceData, ...]`` mirrors the wrapper field for
        # ``*args: FxtrServiceData`` on a typed-service method.
        Wrapper = _build_wrapper(
            "W_FSD_VAR_POS", [("items", tuple[FxtrServiceData, ...])]  # type: ignore[valid-type]
        )
        fid = FxtrID.builtin("psd-var-pos")
        ref = EntityRef(fid, _Foo)
        items = (1, "two", ref)
        encoded = get_basic_encoder(Wrapper, FxtrServicesDefaultDialect).encode(
            Wrapper(items=items)  # type: ignore[call-arg]
        )
        # Each element passes through; the EntityRef is not stripped.
        assert tuple(encoded["items"]) == (1, "two", ref)

    def test_nested_in_dict_value_passes_through(self) -> None:
        # ``dict[str, FxtrServiceData]`` mirrors ``**kwargs: FxtrServiceData``.
        Wrapper = _build_wrapper(
            "W_FSD_VAR_KW", [("flags", dict[str, FxtrServiceData])]  # type: ignore[valid-type]
        )
        fid = FxtrID.builtin("psd-var-kw")
        ref = EntityRef(fid, _Foo)
        flags = {"a": 1, "b": ref, "c": [1, 2]}
        encoded = get_basic_encoder(Wrapper, FxtrServicesDefaultDialect).encode(
            Wrapper(flags=flags)  # type: ignore[call-arg]
        )
        assert encoded["flags"]["a"] == 1
        assert encoded["flags"]["b"] is ref
        assert encoded["flags"]["c"] == [1, 2]


# ---------------------------------------------------------------------------
# encode_to_service_data / decode_from_service_data
# ---------------------------------------------------------------------------


class TestUserConverters:
    def test_primitive_round_trip(self) -> None:
        encoded = encode_to_service_data(42, int)
        assert encoded == 42
        assert decode_from_service_data(42, int) == 42

    def test_entity_ref_round_trip(self) -> None:
        fid = FxtrID.builtin("user-er")
        ref = EntityRef(fid, _Foo)
        encoded = encode_to_service_data(ref, EntityRef[_Foo])
        # Encode strips the target_type to a bare FxtrID.
        assert encoded == fid
        decoded = decode_from_service_data(encoded, EntityRef[_Foo])
        assert isinstance(decoded, EntityRef)
        assert decoded.entity_id == fid
        assert decoded.target_type is _Foo

    def test_frozenset_round_trip(self) -> None:
        encoded = encode_to_service_data(frozenset({1, 2, 3}), frozenset[int])
        assert sorted(encoded) == [1, 2, 3]  # type: ignore[arg-type]
        decoded = decode_from_service_data(encoded, frozenset[int])
        assert decoded == frozenset({1, 2, 3})

    def test_tuple_of_fxtr_id_round_trip(self) -> None:
        ids = (FxtrID.builtin("a"), FxtrID.builtin("b"), FxtrID.builtin("c"))
        encoded = encode_to_service_data(ids, tuple[FxtrID, ...])
        # ``tuple`` encodes to ``list`` per mashumaro defaults.
        assert list(encoded) == list(ids)  # type: ignore[arg-type]
        decoded = decode_from_service_data(encoded, tuple[FxtrID, ...])
        assert decoded == ids

    def test_dataclass_round_trip(self) -> None:
        @dataclasses.dataclass(frozen=True)
        class Box:
            n: int
            label: str

        encoded = encode_to_service_data(Box(n=7, label="hi"), Box)
        assert encoded == {"n": 7, "label": "hi"}
        decoded = decode_from_service_data(encoded, Box)
        assert decoded == Box(n=7, label="hi")

    def test_decode_rejects_non_vocabulary_input(self) -> None:
        # A custom non-vocabulary object can't sit in the input position.
        @dataclasses.dataclass(frozen=True)
        class _Custom:
            n: int

        with pytest.raises(NotFxtrServiceDataError):
            decode_from_service_data(_Custom(1), int)  # type: ignore[arg-type]

    def test_encode_rejects_non_vocabulary_result(self) -> None:
        # Annotation widens to ``Any``; mashumaro happily passes a
        # non-vocabulary value through, and the post-encode validation
        # catches it.
        class Opaque:
            pass

        with pytest.raises(NotFxtrServiceDataError):
            encode_to_service_data(Opaque(), Any)

    def test_dialect_kwarg_override(self) -> None:
        # Custom dialect that adds a strategy for a user type. Verifies
        # ``dialect=`` is actually threaded through both helpers.
        @dataclasses.dataclass(frozen=True)
        class Wrapped:
            n: int

        class _WrappedStrategy(mashumaro_types_strategy()):  # type: ignore[misc]
            def serialize(self, value: Wrapped) -> int:
                return value.n

            def deserialize(self, value: int) -> Wrapped:
                return Wrapped(n=value)

        class CustomDialect(FxtrServicesDefaultDialect):
            serialization_strategy = {
                **FxtrServicesDefaultDialect.serialization_strategy,
                Wrapped: _WrappedStrategy(),
            }

        encoded = encode_to_service_data(Wrapped(n=11), Wrapped, dialect=CustomDialect)
        assert encoded == 11
        decoded = decode_from_service_data(encoded, Wrapped, dialect=CustomDialect)
        assert decoded == Wrapped(n=11)

    def test_codec_cache_reuses_across_calls(self) -> None:
        # Two encode calls on the same (annotated_type, dialect) should
        # hit the cache. Detect by reading the cache size before/after.
        before = get_basic_encoder.cache_info()
        encode_to_service_data(1, int)
        encode_to_service_data(2, int)
        after = get_basic_encoder.cache_info()
        # Total hits incremented by at least 1 (second call was a hit).
        assert after.hits >= before.hits + 1


# ---------------------------------------------------------------------------
# OverrideThisTypeBeforeUsing — encode/decode/resolver guard rails
# ---------------------------------------------------------------------------

_OVERRIDE_RESOLVE_TYPE_ID = FxtrID.builtin("override-placeholder-resolve-test")


class _ResolveMountStubHandle(IRHandle[Any]):
    """Minimal ``IRHandle`` for ``mount_lowlevel_cap_as_service`` tests."""

    def __init__(self, token: LifetimeToken[Any]) -> None:
        self._token = token

    @property
    def token(self) -> LifetimeToken[Any]:
        return self._token

    async def request(
        self,
        lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],
        request: Request,
    ) -> IRValue:
        raise NotImplementedError

    def dispatch(
        self,
        lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],
        request: Request,
    ) -> Any:
        raise NotImplementedError


class TestOverrideThisTypeBeforeUsing:
    def test_encode_to_service_data_raises(self) -> None:
        with pytest.raises(
            TypeError,
            match=re.escape(OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE),
        ):
            encode_to_service_data(42, OverrideThisTypeBeforeUsing)

    def test_decode_from_service_data_raises(self) -> None:
        with pytest.raises(
            TypeError,
            match=re.escape(OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE),
        ):
            decode_from_service_data(42, OverrideThisTypeBeforeUsing)

    @pytest.mark.anyio
    async def test_resolve_service_ref_raises(self) -> None:
        async with FxtrScope(isolated=True) as outer:
            handle = _ResolveMountStubHandle(outer._token)
            outer.track_builtin(_OVERRIDE_RESOLVE_TYPE_ID)
            mounted = outer.mount_lowlevel_cap_as_service(
                handle, UnknownService, _OVERRIDE_RESOLVE_TYPE_ID
            )
            bad_ref = mounted.replace_service_type(OverrideThisTypeBeforeUsing)
            async with FxtrScope(include=[outer], isolated=True) as inner:
                with pytest.raises(
                    TypeError,
                    match=re.escape(OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE),
                ):
                    await bad_ref.resolve(inner)
