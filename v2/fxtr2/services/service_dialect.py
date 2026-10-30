"""Default mashumaro dialect for service-call argument and return conversion.

This module provides :class:`FxtrServicesDefaultDialect`, a
:class:`mashumaro.dialect.Dialect` whose ``serialization_strategy``
covers the fxtr2 vocabulary types that show up in typed-service
method signatures: ``EntityRef[T]``, ``ServiceRef[T]``,
``FxtrID``, plus collection types (``frozenset``, ``set``,
``frozendict``, ``tuple``).

Strategies follow the strict in/out contract from
``part8_plan.md`` §2:

- **Encode** strips type metadata: ``EntityRef[T] → FxtrID``,
  ``ServiceRef[T] → ServiceRef[UnknownService]``.
- **Decode** refuses any input other than the lower-vocabulary
  form (``FxtrID`` / ``ServiceRef[UnknownService]``) and re-attaches
  ``T`` from the field annotation via mashumaro's
  ``GenericSerializableType`` mechanism.

The lower-level :func:`fxtr2.services.service_cap_common._encode_fxtr_service_data`
chain stays untyped; the dialect at *this* layer is the single
source of typed/untyped translation.

The codec cache lives here too: :func:`get_basic_encoder` /
:func:`get_basic_decoder` memoize the per-``(annotation, dialect)``
``BasicEncoder`` / ``BasicDecoder`` build, mirroring the cache
pattern in ``fxtr2.conversion.converters.mashumaro_entities``.

:data:`OverrideThisTypeBeforeUsing` is a ``Never`` type alias used as a
stub annotation before substitution; its dialect strategy and service
resolver raise :exc:`TypeError` until replaced via
:meth:`ServiceRef.replace_service_type` (see
:data:`OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE`).

User-facing helpers :func:`encode_to_service_data` /
:func:`decode_from_service_data` expose the same per-annotation
encode/decode the typed-service layer uses internally, so callers
can convert dynamically when the static annotation isn't available
(e.g. an impl method that takes ``payload: FxtrServiceData`` and
only knows the runtime type). :data:`FxtrServiceData` is registered
in the default dialect as a passthrough strategy, so a typed
Protocol can mix typed slots (``EntityRef[T]`` etc.) with
``FxtrServiceData``-annotated slots that flow through unchanged.
:data:`FxtrServiceData` and :class:`NotFxtrServiceDataError` are
re-exported from :mod:`fxtr2.services.service_cap_common` here for
convenience.
"""

from __future__ import annotations

import dataclasses
import functools
from typing import Any, Callable, Generic, Never, TypeVar

import mashumaro
import mashumaro.codecs.basic
import mashumaro.dialect
import mashumaro.helper
import mashumaro.types
from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    Entity,
    EntityRef,
    ServiceProxyContext,
    ServiceRef,
    register_service_resolver,
)

from .service_cap_common import USE_REMOTE_DEFAULT
from .service_cap_common import FxtrServiceData as FxtrServiceData
from .service_cap_common import NotFxtrServiceDataError as NotFxtrServiceDataError
from .service_cap_common import UnknownService, _UseRemoteDefault, validate_fxtr_service_data

# =============================================================================
# Type variables (module-private)
# =============================================================================


# Bound mirrors ``EntityRef[T_co: Entity]`` from ``entity_ref_scope`` — without
# this bound, Pyright rejects the strategy's ``EntityRef[_T_ER]`` annotations
# because an unbounded TypeVar can't be assigned to ``EntityRef``'s
# ``Entity``-bounded parameter.
_T_ER = TypeVar("_T_ER", bound=Entity)
_T_SR = TypeVar("_T_SR")

type OverrideThisTypeBeforeUsing = Never
"""Placeholder service annotation — unusable until substituted.

Generic service-definition stubs may mention ``OverrideThisTypeBeforeUsing``
where the real protocol type will be filled in at substitution time.
Serialization and resolution deliberately fail until callers replace the
annotation (and any ``ServiceRef`` bearing it) with concrete types via
``ServiceRef.replace_service_type`` / codegen — see
``OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE``.
"""

OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE = (
    "OverrideThisTypeBeforeUsing must be replaced via replace_service_type " "before being used."
)


# =============================================================================
# EntityRef[T] strategy
# =============================================================================


@dataclasses.dataclass(frozen=True)
class _EntityRefServiceHelper(Generic[_T_ER], mashumaro.types.GenericSerializableType):
    """Wire form for an ``EntityRef[T]`` arg/return in a typed service call.

    The wire encoding is just the bare ``FxtrID`` (no ``EntityRef``
    wrapper); the helper's ``_deserialize`` classmethod is what
    receives the ``[T]`` from the field annotation and stashes it on
    the helper instance so the strategy's ``deserialize`` can
    re-attach it.
    """

    entity_id: FxtrID
    target_type: Any = None  # populated by _deserialize from the type-args list

    def _serialize(self, types: list[type]) -> FxtrID:  # noqa: A002 — mashumaro signature
        return self.entity_id

    @classmethod
    def _deserialize(  # noqa: A002 — mashumaro signature
        cls, value: Any, types: list[type]
    ) -> _EntityRefServiceHelper[Any]:
        if isinstance(value, EntityRef):
            raise TypeError(
                "EntityRef field of a typed service method received an "
                "EntityRef value at the dialect layer; the wire should "
                "produce only bare FxtrIDs. The conversion layer is the "
                "single source of typed/untyped translation."
            )
        if not isinstance(value, FxtrID):
            raise TypeError(
                f"EntityRef field of a typed service method expected a "
                f"FxtrID at the dialect layer; got {type(value).__name__}."
            )
        target = types[0] if types else None
        return cls(entity_id=value, target_type=target)


class EntityRefServiceStrategy(mashumaro.types.SerializationStrategy, Generic[_T_ER]):
    """Dialect strategy: ``EntityRef[T]`` ↔ bare ``FxtrID``.

    Encode unconditionally strips ``T`` (produces a bare
    :class:`FxtrID`); decode requires ``FxtrID`` input and re-attaches
    ``T`` from the field annotation via :class:`_EntityRefServiceHelper`.
    """

    def serialize(self, value: EntityRef[_T_ER]) -> _EntityRefServiceHelper[_T_ER]:
        if not isinstance(value, EntityRef):
            raise TypeError(
                f"EntityRefServiceStrategy.serialize expected an EntityRef, "
                f"got {type(value).__name__}."
            )
        return _EntityRefServiceHelper(entity_id=value.entity_id)

    def deserialize(self, value: _EntityRefServiceHelper[_T_ER]) -> EntityRef[_T_ER]:
        target = value.target_type
        if target is None:
            raise TypeError(
                "EntityRefServiceStrategy.deserialize: target type was not "
                "supplied by mashumaro — the field annotation must be "
                "EntityRef[T] for some concrete T."
            )
        return EntityRef(entity_id=value.entity_id, target_type=target)


# =============================================================================
# ServiceRef[T] strategy
# =============================================================================


@dataclasses.dataclass(frozen=True)
class _ServiceRefServiceHelper(Generic[_T_SR], mashumaro.types.GenericSerializableType):
    """Wire form for a ``ServiceRef[T]`` arg/return in a typed service call.

    The wire encoding is the ``ServiceRef[UnknownService]`` produced
    by :mod:`fxtr2.services.service_cap_common` (handle + service_type_id);
    the helper's ``_deserialize`` retypes to ``ServiceRef[T]`` using the
    ``[T]`` from the field annotation.
    """

    ref: ServiceRef[Any]
    target_type: Any = None  # populated by _deserialize from the type-args list

    def _serialize(self, types: list[type]) -> ServiceRef[Any]:  # noqa: A002
        return self.ref

    @classmethod
    def _deserialize(  # noqa: A002
        cls, value: Any, types: list[type]
    ) -> _ServiceRefServiceHelper[Any]:
        if not isinstance(value, ServiceRef):
            raise TypeError(
                f"ServiceRef field of a typed service method expected a "
                f"ServiceRef at the dialect layer; got {type(value).__name__}."
            )
        if value.target_type is not UnknownService:
            raise TypeError(
                "ServiceRef field of a typed service method received a "
                f"ServiceRef[{value.target_type!r}] at the dialect layer; "
                "the wire should produce only ServiceRef[UnknownService]. "
                "The conversion layer is the single source of typed/untyped "
                "translation."
            )
        target = types[0] if types else None
        return cls(ref=value, target_type=target)


class ServiceRefServiceStrategy(mashumaro.types.SerializationStrategy, Generic[_T_SR]):
    """Dialect strategy: ``ServiceRef[T]`` ↔ ``ServiceRef[UnknownService]``.

    Encode strips ``T`` (produces ``ServiceRef[UnknownService]`` via
    :meth:`ServiceRef.replace_service_type`); decode requires a
    ``ServiceRef[UnknownService]`` and re-attaches ``T``.
    """

    def serialize(self, value: ServiceRef[_T_SR]) -> _ServiceRefServiceHelper[_T_SR]:
        if not isinstance(value, ServiceRef):
            raise TypeError(
                f"ServiceRefServiceStrategy.serialize expected a ServiceRef, "
                f"got {type(value).__name__}."
            )
        # Strip target_type so the wire form is uniform.
        bare = value.replace_service_type(UnknownService)
        return _ServiceRefServiceHelper(ref=bare)

    def deserialize(self, value: _ServiceRefServiceHelper[_T_SR]) -> ServiceRef[_T_SR]:
        target = value.target_type
        if target is None:
            raise TypeError(
                "ServiceRefServiceStrategy.deserialize: target type was not "
                "supplied by mashumaro — the field annotation must be "
                "ServiceRef[T] for some concrete T."
            )
        return value.ref.replace_service_type(target)


# =============================================================================
# Collection strategies (frozenset, set, frozendict, tuple)
# =============================================================================


_T_COLL = TypeVar("_T_COLL")
_K_COLL = TypeVar("_K_COLL")
_V_COLL = TypeVar("_V_COLL")


class FrozensetServiceStrategy(mashumaro.types.SerializationStrategy, Generic[_T_COLL]):
    """``frozenset[X]`` ↔ ``list[X]`` (no canonical ordering — service calls
    are point-to-point, no canonicalization needed)."""

    def serialize(self, value: frozenset[_T_COLL]) -> list[_T_COLL]:
        return list(value)

    def deserialize(self, value: list[_T_COLL]) -> frozenset[_T_COLL]:
        return frozenset(value)


class SetServiceStrategy(mashumaro.types.SerializationStrategy, Generic[_T_COLL]):
    """``set[X]`` ↔ ``list[X]``."""

    def serialize(self, value: set[_T_COLL]) -> list[_T_COLL]:
        return list(value)

    def deserialize(self, value: list[_T_COLL]) -> set[_T_COLL]:
        return set(value)


class FrozendictServiceStrategy(mashumaro.types.SerializationStrategy, Generic[_K_COLL, _V_COLL]):
    """``frozendict[K, V]`` ↔ ``dict[K, V]``."""

    def serialize(self, value: frozendict[_K_COLL, _V_COLL]) -> dict[_K_COLL, _V_COLL]:
        return dict(value)

    def deserialize(self, value: dict[_K_COLL, _V_COLL]) -> frozendict[_K_COLL, _V_COLL]:
        return frozendict(value)


class FxtrIDServiceStrategy(mashumaro.types.SerializationStrategy):
    """Passthrough strategy for :class:`FxtrID`.

    The lower-level :data:`FxtrServiceData` vocabulary already treats
    ``FxtrID`` as a leaf (encoded to ``IRTagged(FXTR_ID_TAG, bytes)``
    by ``service_cap_common``); at the dialect layer we just hand it
    through unchanged so mashumaro doesn't try to serialize its
    internal fields.
    """

    def serialize(self, value: FxtrID) -> FxtrID:
        if not isinstance(value, FxtrID):
            raise TypeError(
                f"FxtrIDServiceStrategy.serialize expected a FxtrID, "
                f"got {type(value).__name__}."
            )
        return value

    def deserialize(self, value: FxtrID) -> FxtrID:
        if not isinstance(value, FxtrID):
            raise TypeError(
                f"FxtrIDServiceStrategy.deserialize expected a FxtrID, "
                f"got {type(value).__name__}."
            )
        return value


class OverridePlaceholderServiceStrategy(mashumaro.types.SerializationStrategy):
    """Fails encode/decode — :data:`OverrideThisTypeBeforeUsing` is not wire-safe."""

    def serialize(self, value: Any) -> Any:
        raise TypeError(OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE)

    def deserialize(self, value: Any) -> Any:
        raise TypeError(OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE)


# =============================================================================
# FxtrServicesDefaultDialect
# =============================================================================


class FxtrServicesDefaultDialect(mashumaro.dialect.Dialect):
    """Default dialect for typed service methods.

    Subclass to extend with additional strategies (the
    :class:`mashumaro.dialect.Dialect.merge` API composes
    serialization_strategy dicts).
    """

    serialization_strategy = {
        EntityRef: EntityRefServiceStrategy(),
        ServiceRef: ServiceRefServiceStrategy(),
        FxtrID: FxtrIDServiceStrategy(),
        frozenset: FrozensetServiceStrategy(),
        set: SetServiceStrategy(),
        frozendict: FrozendictServiceStrategy(),
        # ``USE_REMOTE_DEFAULT`` flows through unchanged — the typed
        # service layer strips it from the encoded dict post-mashumaro
        # so it never reaches the wire (and on the receive side, its
        # presence in a decoded wrapper signals "the proxy didn't ship
        # this; let Python's call machinery apply the impl's default").
        _UseRemoteDefault: mashumaro.helper.pass_through,
        # ``FxtrServiceData`` flows through unchanged — a parameter or
        # return annotated ``FxtrServiceData`` opts out of dialect-level
        # conversion entirely. This lets typed Protocols mix typed slots
        # (``EntityRef[T]`` etc., which the dialect strips/re-attaches)
        # with untyped slots that travel as bare ``FxtrServiceData``.
        # Mashumaro keys strategy lookup directly on the field type and
        # only unwraps ``TypeAliasType`` to ``__value__`` as a fallback,
        # so the registration works for nested occurrences too
        # (e.g. ``tuple[FxtrServiceData, ...]`` for ``*args: FxtrServiceData``).
        FxtrServiceData: mashumaro.helper.pass_through,
        OverrideThisTypeBeforeUsing: OverridePlaceholderServiceStrategy(),
        # ``tuple`` and primitives are handled by mashumaro's built-ins.
    }


# =============================================================================
# Codec cache
# =============================================================================


@functools.cache
def get_basic_encoder(
    shape_type: Any,
    dialect: type[mashumaro.dialect.Dialect],
    post_encoder_func: Callable[[Any], Any] | None = None,
) -> mashumaro.codecs.basic.BasicEncoder[Any]:
    """Build (or return cached) ``BasicEncoder`` for ``(shape_type, dialect)``.

    Cache key is hashable by virtue of all three args being hashable
    (types / classes / function refs).
    """
    return mashumaro.codecs.basic.BasicEncoder(
        shape_type, default_dialect=dialect, post_encoder_func=post_encoder_func
    )


@functools.cache
def get_basic_decoder(
    shape_type: Any,
    dialect: type[mashumaro.dialect.Dialect],
    pre_decoder_func: Callable[[Any], Any] | None = None,
) -> mashumaro.codecs.basic.BasicDecoder[Any]:
    """Build (or return cached) ``BasicDecoder`` for ``(shape_type, dialect)``."""
    return mashumaro.codecs.basic.BasicDecoder(
        shape_type, default_dialect=dialect, pre_decoder_func=pre_decoder_func
    )


# =============================================================================
# User-facing converters
# =============================================================================


def encode_to_service_data(
    value: Any,
    annotated_type: Any,
    *,
    dialect: type[mashumaro.dialect.Dialect] = FxtrServicesDefaultDialect,
) -> FxtrServiceData:
    """Encode ``value`` against ``annotated_type`` to a :data:`FxtrServiceData`-shaped form.

    Builds (or fetches from the codec cache) a :class:`mashumaro.codecs.basic.BasicEncoder`
    for ``(annotated_type, dialect)`` and applies it to ``value``. The
    same dialect-driven conversion the typed-service layer uses for
    method args / return values, exposed as a free function so callers
    can encode dynamically when the static annotation isn't available
    (e.g. inside an impl method that received an ``FxtrServiceData``
    parameter and only knows the runtime type).

    Validates the encoded result against the :data:`FxtrServiceData`
    vocabulary before returning so a mis-annotated value (something
    mashumaro happily encodes into a non-vocabulary shape) fails here
    rather than silently going to the wire.

    Args:
        value: The value to encode.
        annotated_type: The static annotation to encode against —
            anything mashumaro accepts at the field-type position
            (e.g. ``int``, ``EntityRef[MyEntity]``, ``frozenset[str]``,
            ``list[ServiceRef[MyProto]]``).
        dialect: The mashumaro dialect that drives the conversion.
            Default: :class:`FxtrServicesDefaultDialect`.

    Raises:
        NotFxtrServiceDataError: The encoded result isn't
            :data:`FxtrServiceData`-shaped (typically: the annotation
            didn't capture some leaf type and mashumaro produced a
            non-vocabulary value).
    """
    encoder = get_basic_encoder(annotated_type, dialect)
    encoded = encoder.encode(value)
    validate_fxtr_service_data(encoded, where="encode_to_service_data")
    return encoded


def decode_from_service_data(
    value: FxtrServiceData,
    annotated_type: Any,
    *,
    dialect: type[mashumaro.dialect.Dialect] = FxtrServicesDefaultDialect,
) -> Any:
    """Decode a :data:`FxtrServiceData`-shaped ``value`` against ``annotated_type``.

    Builds (or fetches from the codec cache) a :class:`mashumaro.codecs.basic.BasicDecoder`
    for ``(annotated_type, dialect)`` and applies it to ``value``.
    Inverse of :func:`encode_to_service_data`.

    Validates the input against the :data:`FxtrServiceData` vocabulary
    before invoking the decoder so garbage input fails with a clean
    error rather than a deep mashumaro stack trace.

    Args:
        value: The :data:`FxtrServiceData`-shaped value to decode.
        annotated_type: The static annotation to decode into.
        dialect: The mashumaro dialect that drives the conversion.
            Default: :class:`FxtrServicesDefaultDialect`.

    Raises:
        NotFxtrServiceDataError: ``value`` isn't
            :data:`FxtrServiceData`-shaped.
    """
    validate_fxtr_service_data(value, where="decode_from_service_data")
    decoder = get_basic_decoder(annotated_type, dialect)
    return decoder.decode(value)


async def _resolve_override_placeholder(
    context: ServiceProxyContext,
    target_type: Any,
) -> Any:
    del context, target_type
    raise TypeError(OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE)


register_service_resolver(
    OverrideThisTypeBeforeUsing,
    _resolve_override_placeholder,
    allow_implicit_overrides=True,
)
