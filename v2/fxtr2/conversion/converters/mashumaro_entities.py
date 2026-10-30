"""Adapters for defining fxtr entities using mashumaro."""

from __future__ import annotations

import contextvars
import dataclasses
import functools
import typing
from typing import Any, Callable, Generic, NoReturn, Self

import mashumaro
import mashumaro.codecs
import mashumaro.config
import mashumaro.helper
import mashumaro.types

from fxtr2.data.entity_core import (
    MISSING_FXTR_CTX_SENTINEL,
    DuplicateEntityMarkerError,
    FrozenFxtrCbor,
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawFxtrEntityMissingMarkerSentinel,
    RawStrongFxtrID,
    RawWeakFxtrID,
    freeze_fxtr_cbor,
    thaw_fxtr_cbor,
)
from fxtr2.data.entity_ref_scope import (
    DeserializedEntity,
    Entity,
    EntityLoadHandler,
    EntityRef,
    EntityTypeAnnotation,
    get_active_scope,
    register_entity_load_handler,
)

# Type aliases for EntityRef link strength
type ChildRef[T: Entity] = EntityRef[T]
"""An EntityRef that represents a strong (ownership) link to another entity."""


# =============================================================================
# Recursive Serialization/Deserialization Infrastructure
# =============================================================================


@dataclasses.dataclass(frozen=True)
class DeferredLoadPlaceholder:
    """Sentinel returned during the collecting phase of two-phase deserialization.

    When deserializing an inline entity (Child[T] or LinkedEntity[T]), the first phase
    returns this placeholder instead of the actual entity. The placeholder tracks which
    entity ref it corresponds to so the rebuild phase can substitute the loaded entity.
    """

    index: int
    """Index into the collected list for this placeholder."""


@dataclasses.dataclass
class RecursiveLoadCollecting:
    """State during phase 1 (collecting) of two-phase deserialization.

    During this phase, inline entity fields collect their EntityRefs and return
    DeferredLoadPlaceholder sentinels instead of loaded entities.
    """

    collected: list[tuple[EntityRef[Any], Any]]
    """List of (entity_ref, type_annotation) pairs collected during deserialization."""


@dataclasses.dataclass
class RecursiveLoadRebuilding:
    """State during phase 2 (rebuilding) of two-phase deserialization.

    During this phase, inline entity fields return the pre-loaded entities
    instead of placeholders. The collected list is kept for validation.
    """

    entities: list[Any]
    """Loaded entities, parallel to the collected list from phase 1."""
    collected: list[tuple[EntityRef[Any], Any]]
    """Original collected list for validation."""
    next_index: int = 0
    """Current index into entities list."""


@dataclasses.dataclass
class RecursiveStore:
    """State during serialization of a DataclassEntity.

    This tracks the scope being stored into so inline entity serialization
    can store child entities into the correct scope.
    """


# Context variable for tracking recursive serialization/deserialization state
RECURSIVE_SERDES_STATE: contextvars.ContextVar[
    RecursiveLoadCollecting | RecursiveLoadRebuilding | RecursiveStore | None
] = contextvars.ContextVar("recursive_serdes_state", default=None)

type ChildFxtrID = FxtrID
"""A FxtrID that represents a strong (ownership) link to another entity."""

type Child[T: Entity] = T
"""An inline entity with a strong (ownership) link. Auto-loaded during deserialization."""

type LinkedEntity[T: Entity] = T
"""An inline entity with a weak link. Auto-loaded during deserialization."""


# for some reason this only works with old-style type vars
_T_InlineEntity = typing.TypeVar("_T_InlineEntity", bound=Entity)


@dataclasses.dataclass(frozen=True)
class _InlineEntitySerializationHelper(
    typing.Generic[_T_InlineEntity], mashumaro.types.GenericSerializableType
):
    """Helper for inline entity serialization that preserves type information.

    This class uses GenericSerializableType to receive type arguments during
    deserialization, allowing InlineEntitySerializationStrategy to build
    converters for the correct concrete type.
    """

    value: _T_InlineEntity
    strong: bool

    def _serialize(self, types: list[type[_T_InlineEntity]]) -> RawStrongFxtrID | RawWeakFxtrID:
        assert len(types) == 1
        target_type = types[0]

        if target_type is typing.Any:
            # Store it as the type of the value (matches default behavior of scope.store)
            target_type = type(self.value)

        state = RECURSIVE_SERDES_STATE.get()
        if not isinstance(state, RecursiveStore):
            raise RuntimeError(
                "Cannot serialize inline entity outside of DataclassEntity serialization context. "
                "Make sure you're using DataclassEntity._to_fxtr_cbor() or store() method."
            )

        # Temporarily reset state before storing child to get fresh context
        token = RECURSIVE_SERDES_STATE.set(None)
        try:
            ref = get_active_scope().store(self.value)
        finally:
            RECURSIVE_SERDES_STATE.reset(token)

        if self.strong:
            return RawStrongFxtrID(inner_id=ref.entity_id)
        else:
            return RawWeakFxtrID(inner_id=ref.entity_id)

    @classmethod
    def _deserialize(
        cls, value: RawStrongFxtrID | RawWeakFxtrID, types: list[type[_T_InlineEntity]]
    ) -> Self:
        raise NotImplementedError("Cannot serialize inline entity deserialization helper")


@dataclasses.dataclass(frozen=True)
class _InlineEntityDeserializationHelper(
    typing.Generic[_T_InlineEntity], mashumaro.types.GenericSerializableType
):
    """Helper for inline entity deserialization that preserves type information.

    This class uses GenericSerializableType to receive type arguments during
    deserialization, allowing _InlineEntityDeserializationHelper to build
    converters for the correct concrete type.
    """

    fxtr_id: FxtrID
    target_type: type[_T_InlineEntity]
    strong: bool

    def _serialize(self, types: list[type[_T_InlineEntity]]) -> RawStrongFxtrID | RawWeakFxtrID:
        raise NotImplementedError("Cannot serialize inline entity deserialization helper")

    @classmethod
    def _deserialize(
        cls, value: RawStrongFxtrID | RawWeakFxtrID, types: list[type[_T_InlineEntity]]
    ) -> Self:
        strong = isinstance(value, RawStrongFxtrID)
        assert len(types) == 1
        return cls(fxtr_id=value.inner_id, target_type=types[0], strong=strong)


@dataclasses.dataclass(frozen=True)
class InlineEntitySerializationStrategy(
    mashumaro.types.SerializationStrategy, typing.Generic[_T_InlineEntity]
):
    """Serialization strategy for Child[T] and LinkedEntity[T] types.

    This strategy enables inline entities to be stored/loaded automatically
    during DataclassEntity serialization/deserialization.
    """

    strong: bool

    def serialize(
        self, value: _T_InlineEntity
    ) -> _InlineEntitySerializationHelper[_T_InlineEntity]:
        """Store the entity and return a helper with its ID."""
        return _InlineEntitySerializationHelper(value=value, strong=self.strong)

    def deserialize(
        self, value: _InlineEntityDeserializationHelper[_T_InlineEntity]
    ) -> _T_InlineEntity | DeferredLoadPlaceholder:
        """Deserialize by collecting ref (phase 1) or returning loaded entity (phase 2)."""
        fxtr_id = value.fxtr_id
        target_type = value.target_type
        assert (
            target_type is not None
        ), "target_type should be set by _InlineEntityDeserializationHelper._deserialize"

        if target_type is typing.Any:
            raise TypeError(
                "Cannot deserialize a Child[Any] or LinkedEntity[Any]: deserialization needs a"
                " concrete target type! (This can happen if you try to deserialize an object with a"
                " field that is a generic Child[T] or LinkedEntity[T] without specifying the"
                " concrete target type during deserialization.)"
            )

        state = RECURSIVE_SERDES_STATE.get()

        if isinstance(state, RecursiveLoadCollecting):
            # Phase 1: collect the ref and return a placeholder
            ref = EntityRef(fxtr_id, target_type)
            index = len(state.collected)
            state.collected.append((ref, target_type))
            return DeferredLoadPlaceholder(index=index)

        elif isinstance(state, RecursiveLoadRebuilding):
            # Phase 2: return the loaded entity with validation
            index = state.next_index
            state.next_index += 1

            # Validate that we're returning the same ref we collected
            collected_ref, collected_type = state.collected[index]
            if collected_ref.entity_id != fxtr_id:
                raise RuntimeError(
                    f"Deserialization order mismatch at index {index}: "
                    f"collected {collected_ref.entity_id} but rebuilding got {fxtr_id}"
                )

            # Validate that the type annotation matches
            if collected_type != target_type:
                raise RuntimeError(
                    f"Type mismatch at index {index}: "
                    f"collected {collected_type} but rebuilding got {target_type}"
                )

            return state.entities[index]  # type: ignore[return-value]

        else:
            raise RuntimeError(
                "Cannot deserialize inline entity outside of DataclassEntity deserialization"
                " context. Make sure you're loading via EntityRef.load() with a DataclassEntity."
            )


@dataclasses.dataclass(frozen=True)
class _EntityRefSerializationHelper[T](mashumaro.types.GenericSerializableType):
    ref: FxtrID
    target_type: typing.Type[T] | None
    strong: bool

    def _serialize(self, types) -> RawStrongFxtrID | RawWeakFxtrID:
        assert len(types) == 1
        if self.strong:
            return RawStrongFxtrID(inner_id=self.ref)
        else:
            return RawWeakFxtrID(inner_id=self.ref)

    @classmethod
    def _deserialize(
        cls, value: RawStrongFxtrID | RawWeakFxtrID, types: list[typing.Type[T]]
    ) -> Self:
        strong = isinstance(value, RawStrongFxtrID)
        assert len(types) == 1
        target_type = types[0]
        return cls(ref=value.inner_id, target_type=target_type, strong=strong)


# for some reason this only works with old-style type vars
_T_ERefSerStrat = typing.TypeVar("_T_ERefSerStrat", bound=Entity)


@dataclasses.dataclass(frozen=True)
class EntityRefSerializationStrategy(
    mashumaro.types.SerializationStrategy, typing.Generic[_T_ERefSerStrat]
):
    strong: bool

    def serialize(
        self, value: EntityRef[_T_ERefSerStrat]
    ) -> _EntityRefSerializationHelper[_T_ERefSerStrat]:
        helper = _EntityRefSerializationHelper(
            ref=value.entity_id, target_type=None, strong=self.strong
        )
        return helper

    def deserialize(
        self, value: _EntityRefSerializationHelper[_T_ERefSerStrat]
    ) -> EntityRef[_T_ERefSerStrat]:
        assert self.strong == value.strong
        assert value.target_type is not None
        if value.target_type is typing.Any:
            raise TypeError(
                "Cannot deserialize an EntityRef[Any]: EntityRef needs a concrete target type!"
                " (This can happen if you try to deserialize an object with a field that is a"
                " generic EntityRef[T] without specifying the concrete target type during"
                " deserialization.)"
            )
        return EntityRef(entity_id=value.ref, target_type=value.target_type)


@dataclasses.dataclass(frozen=True)
class FxtrIDSerializationStrategy(mashumaro.types.SerializationStrategy):
    strong: bool

    def serialize(self, value: FxtrID) -> RawStrongFxtrID | RawWeakFxtrID:
        assert isinstance(value, FxtrID)
        if self.strong:
            return RawStrongFxtrID(inner_id=value)
        else:
            return RawWeakFxtrID(inner_id=value)

    def deserialize(self, value: RawStrongFxtrID | RawWeakFxtrID) -> FxtrID:
        if self.strong:
            assert isinstance(value, RawStrongFxtrID)
        else:
            assert isinstance(value, RawWeakFxtrID)
        return value.inner_id


# =============================================================================
# Collection Serialization Strategies (frozendict, set, frozenset)
# =============================================================================

from frozendict import frozendict


def _canonical_set_sort_key(item: Any) -> Any:
    """Return a sort key for canonical ordering of set/frozenset elements.

    All elements must be the same recognized type. Recognized types are: str, int, bytes,
    FxtrID, and EntityRef.
    """
    if isinstance(item, FxtrID):
        return item._raw_data_id
    elif isinstance(item, EntityRef):
        return item.entity_id._raw_data_id
    elif isinstance(item, (str, bytes, int)):
        return item
    else:
        raise ValueError(
            "Default set canonicalization order only supports string, int, bytes, FxtrID, and"
            f" EntityRef items, but got {type(item)}. If you need canonical serialization of other"
            " types, you should override set serialization."
        )


def _validate_homogeneous_set(items: set | frozenset) -> None:
    """Validate that all elements in a set are the same recognized type."""
    if not items:
        return
    first = next(iter(items))
    if isinstance(first, FxtrID):
        expected = FxtrID
    elif isinstance(first, EntityRef):
        expected = EntityRef
    elif isinstance(first, str):
        expected = str
    elif isinstance(first, bytes):
        expected = bytes
    elif isinstance(first, int):
        expected = int
    else:
        raise ValueError(
            "Default set canonicalization order only supports string, int, bytes, FxtrID, and"
            f" EntityRef items, but got {type(first)}. If you need canonical serialization of other"
            " types, you should override set serialization."
        )
    for item in items:
        if not isinstance(item, expected):
            raise ValueError(
                f"Set contains mixed types: expected all {expected.__name__}, but found"
                f" {type(item).__name__}. Sets must be homogeneous for canonical serialization."
            )
    if expected is EntityRef:
        unique_ids = {item.entity_id for item in items}  # type: ignore[union-attr]
        if len(unique_ids) != len(items):
            raise ValueError(
                "Set of EntityRefs contains duplicate entity IDs with different targets."
                " Each EntityRef in a set must have a unique entity_id."
            )


# more explicit mashumaro generic type vars
_T_m1 = typing.TypeVar("_T_m1")
_T_m2 = typing.TypeVar("_T_m2")


class FrozendictSerializationStrategy(mashumaro.types.SerializationStrategy, Generic[_T_m1, _T_m2]):
    """Simple frozendict serialization - just convert to/from dict."""

    def serialize(self, value: frozendict[_T_m1, _T_m2]) -> dict[_T_m1, _T_m2]:
        return dict(value)

    def deserialize(self, value: dict[_T_m1, _T_m2]) -> frozendict[_T_m1, _T_m2]:
        return frozendict(value)


class SetSerializationStrategy(mashumaro.types.SerializationStrategy, Generic[_T_m1]):
    """Set serialization with canonical ordering."""

    def serialize(self, value: set[_T_m1]) -> list[_T_m1]:
        _validate_homogeneous_set(value)
        return sorted(value, key=_canonical_set_sort_key)

    def deserialize(self, value: list[_T_m1]) -> set[_T_m1]:
        return set(value)


class FrozensetSerializationStrategy(mashumaro.types.SerializationStrategy, Generic[_T_m1]):
    """Frozenset serialization with canonical ordering."""

    def serialize(self, value: frozenset[_T_m1]) -> list[_T_m1]:
        _validate_homogeneous_set(value)
        return sorted(value, key=_canonical_set_sort_key)

    def deserialize(self, value: list[_T_m1]) -> frozenset[_T_m1]:
        return frozenset(value)


class FrozenFxtrCborSerializationStrategy(mashumaro.types.SerializationStrategy):
    """Round-trip a ``FrozenFxtrCbor`` field through the entity cbor.

    Serialize: thaw to ``FxtrCbor`` (frozendict → dict, tuple → list)
    so the value blends into the entity dict that ``store_cbor`` and
    ``visit_ids`` consume — both operate on the mutable form. The
    raw-id wrappers (``RawWeakFxtrID`` / ``RawStrongFxtrID`` /
    ``RawFxtrEntityMarkerID``) ride through verbatim — they're what
    ``fxtr_cbor_dumps`` recognizes at the cbor layer.

    Deserialize: freeze the round-tripped cbor back to a
    ``FrozenFxtrCbor`` so the Python-level field remains immutable
    and hashable (matches the ``frozen=True`` dataclass invariant).
    """

    def serialize(self, value: FrozenFxtrCbor) -> FxtrCbor:
        return thaw_fxtr_cbor(value)

    def deserialize(self, value: FxtrCbor) -> FrozenFxtrCbor:
        return freeze_fxtr_cbor(value)


# =============================================================================
# Dataclass entity base class
# =============================================================================


def _bad_fxtr_type() -> NoReturn:
    raise ValueError(
        "No value was provided for fxtr_type in a DataclassEntity subclass! Usually a default value"
        " should be provided either in the class definition or as a class keyword argument."
    )


# Field metadata flag: ``mashumaro`` skips a field marked this way when packing,
# unconditionally. We use it on the framework-managed fields (``fxtr_type`` /
# ``fxtr_ctx``) so mashumaro never serializes them — both because the
# ``fxtr_type`` default-factory (``_bad_fxtr_type``) must never be evaluated, and
# so that the fields survive regardless of ``omit_default`` (their values always
# equal their defaults, which an entity opting into ``omit_default`` would
# otherwise strip). The fields are re-injected by ``DataclassEntity.__post_serialize__``.
_OMIT_FROM_MASHUMARO: typing.Final = {"serialize": "omit"}


@dataclasses.dataclass(frozen=True, kw_only=True)
class DataclassEntity(mashumaro.DataClassDictMixin, Entity):
    """Base class for entities that are represented as mashumaro-serializable dataclasses.

    Mashumaro provides strong support for extensible serialization and deserialization. This base
    class provides a convenient way to create entities that are represented as
    mashumaro-serializable dataclasses.

    Subclasses must set the fxtr_type class attribute to the FxtrID of the entity type. Tagged
    decoding — resolving a stored ``$fxtr.type`` to the right concrete Python type — is handled by
    the load machinery in ``entity_ref_scope`` (via ``DataclassEntityLoadHandler.supports_entity_type``),
    not by a mashumaro discriminator; ``__post_init__`` validates the decoded ``fxtr_type`` against
    the expected ``_fxtr_known_type_id``.
    """

    fxtr_type: typing.Final[FxtrID] = dataclasses.field(
        default_factory=_bad_fxtr_type,
        repr=False,
        kw_only=True,
        metadata=_OMIT_FROM_MASHUMARO,
    )
    fxtr_ctx: typing.Final[RawFxtrEntityMarkerID | RawFxtrEntityMissingMarkerSentinel] = (
        dataclasses.field(
            default=MISSING_FXTR_CTX_SENTINEL,
            compare=False,
            repr=False,
            kw_only=True,
            metadata=_OMIT_FROM_MASHUMARO,
        )
    )

    _fxtr_known_type_id: typing.ClassVar[FxtrID | None] = None

    class Config(mashumaro.config.BaseConfig):
        aliases = {"fxtr_type": "$fxtr.type", "fxtr_ctx": "$fxtr.ctx"}
        serialize_by_alias = True
        # Reject unknown keys on deserialization. This ensures that attempting to _load_ a value
        # with extra unrecognized fields fails by default, to avoid accidental data loss.
        forbid_extra_keys = True
        # ``omit_default`` (drop fields equal to their default on serialization) is left OFF by
        # default: default-valued fields are serialized like any other. An individual entity can
        # opt in with ``class Config(DataclassEntity.Config): omit_default = True`` — the
        # framework-managed fields survive it because they are spliced in by
        # ``__post_serialize__`` rather than serialized by mashumaro (see ``_OMIT_FROM_MASHUMARO``).
        # No mashumaro ``discriminator``: tagged decoding (resolving the stored
        # ``$fxtr.type`` to a concrete Python type) is done by the load machinery
        # in ``entity_ref_scope`` (``find_handler_or_union_handlers`` +
        # ``DataclassEntityLoadHandler.supports_entity_type``), which only ever
        # hands mashumaro a concrete target type. ``__post_init__`` validates the
        # decoded ``fxtr_type`` against ``_fxtr_known_type_id`` as the safety net.
        # Keeping a Config-level discriminator here would also make every entity
        # that overrides ``Config`` re-root the discriminated hierarchy at itself
        # and fail to decode (``SuitableVariantNotFoundError``).
        serialization_strategy = {
            # FxtrID: weak by default, ChildFxtrID for strong links
            FxtrID: FxtrIDSerializationStrategy(strong=False),
            ChildFxtrID: FxtrIDSerializationStrategy(strong=True),
            # EntityRef: weak by default, ChildRef for strong links
            EntityRef: EntityRefSerializationStrategy(strong=False),
            ChildRef: EntityRefSerializationStrategy(strong=True),
            # Inline entities: auto-loaded during deserialization
            Child: InlineEntitySerializationStrategy(strong=True),
            LinkedEntity: InlineEntitySerializationStrategy(strong=False),
            # Pass through for context markers (and the sentinel default —
            # mashumaro encodes it untouched; the cbor encoder hook in
            # ``fxtr_cbor_dumps_with_marker_check`` rejects sentinels at
            # store time, and ``_fxtr_store_impl`` replaces the top-level
            # sentinel with the destination scope's marker before storage).
            RawFxtrEntityMarkerID: mashumaro.helper.pass_through,
            RawFxtrEntityMissingMarkerSentinel: mashumaro.helper.pass_through,
            # Raw bytes survive both CBOR (which has a native byte-string
            # type) and the workstation's wire_escape (which tags them
            # as ``["bytes", "<base64>"]``). Mashumaro's default is to
            # base64-encode bytes to ``str``, which would round-trip
            # through storage as a plain string and lose the bytes
            # identity on the wire — frontends see ``str`` instead of
            # ``Uint8Array``.
            bytes: mashumaro.helper.pass_through,
            # ``FrozenFxtrCbor`` (recursive union including frozendict
            # / tuple / primitives / Raw{Weak,Strong}FxtrID /
            # RawFxtrEntityMarkerID) is mashumaro-opaque: at the cbor
            # layer ``fxtr_cbor_dumps`` is what understands the raw-id
            # wrappers, and the rest of the storage pipeline
            # (``store_cbor`` / ``visit_ids``) operates on the mutable
            # form. The strategy thaws on serialize so the value
            # blends into the surrounding ``FxtrCbor`` entity dict,
            # and freezes on deserialize so the Python-level field
            # stays immutable / hashable.
            FrozenFxtrCbor: FrozenFxtrCborSerializationStrategy(),
            # Collection types
            frozendict: FrozendictSerializationStrategy(),
            set: SetSerializationStrategy(),
            frozenset: FrozensetSerializationStrategy(),
        }

    def __init_subclass__(cls, fxtr_type: FxtrID | None = None, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if fxtr_type is not None:
            if "fxtr_type" in cls.__annotations__ or "fxtr_type" in cls.__dict__:
                raise ValueError("Cannot set fxtr_type as class keyword argument and also field!")
            cls.__annotations__["fxtr_type"] = typing.Final[FxtrID]
            cls.fxtr_type = dataclasses.field(  # type: ignore
                default=fxtr_type, repr=False, kw_only=True, metadata=_OMIT_FROM_MASHUMARO
            )
            cls._fxtr_known_type_id = fxtr_type
        elif "fxtr_type" in cls.__dict__:
            if "fxtr_type" not in cls.__annotations__:
                raise ValueError(
                    "When overriding fxtr_type as a class attribute, it must be annotated as a"
                    " dataclass field!"
                )
            tyval = cls.__dict__["fxtr_type"]
            if isinstance(tyval, FxtrID):
                cls._fxtr_known_type_id = tyval
            elif isinstance(tyval, dataclasses.Field) and isinstance(tyval.default, FxtrID):
                cls._fxtr_known_type_id = tyval.default
            else:
                raise ValueError(
                    "fxtr_type must be a FxtrID or a dataclasses.Field with a default value of a"
                    " FxtrID"
                )
        else:
            cls._fxtr_known_type_id = None

    def __post_init__(self) -> None:
        expected = self._fxtr_known_type_id
        if expected is not None and self.fxtr_type != expected:
            raise ValueError(f"Expected fxtr_type {expected} but got {self.fxtr_type}")

    def __post_serialize__(self, d: dict[str, FxtrCbor]) -> FxtrCbor:  # type: ignore[reportIncompatibleMethodOverride]
        """Inject the framework-managed fields after mashumaro builds the dict.

        ``fxtr_type`` / ``fxtr_ctx`` carry ``metadata=_OMIT_FROM_MASHUMARO`` so
        mashumaro never serializes them itself (which keeps ``omit_default`` from
        stripping the type tag / marker, and avoids evaluating the
        ``_bad_fxtr_type`` default-factory at class-compile time). This hook restores them.

        Emitting the sentinel for *every* entity — including one that was inlined
        as a plain field instead of being wrapped in ``Child[T]`` /
        ``LinkedEntity[T]`` — is what lets ``fxtr_cbor_dumps_with_marker_check``
        detect illegal bare nesting.
        """
        d["$fxtr.type"] = RawWeakFxtrID(inner_id=self.fxtr_type)
        # ``fxtr_ctx`` may still be the missing-marker sentinel, which is not a
        # ``FxtrCbor`` value but is passed through by the serialization strategy
        # (see ``Config.serialization_strategy``) so the marker check can detect it.
        d["$fxtr.ctx"] = self.fxtr_ctx  # type: ignore[reportArgumentType]
        return d

    def _fxtr_convert_to_cbor(self) -> FxtrCbor:
        """Build the entity's CBOR form, applying the $fxtr.ctx override.

        Pure conversion — no storage. The cbor is suitable for handing
        to ``scope.store_cbor`` (which is what ``_fxtr_store_impl``
        does) or to other storage paths that consume cbor directly
        (e.g. ``scope.create_c_node`` for minting a c-node out of a
        DataclassEntity-shaped payload).

        Builds the cbor via mashumaro's ``to_dict`` (``__post_serialize__``
        injects ``$fxtr.type`` / ``$fxtr.ctx``), then replaces the
        *top-level* ``$fxtr.ctx`` slot with the active scope's marker iff
        it still carries ``MISSING_FXTR_CTX_SENTINEL`` (i.e. ``fxtr_ctx``
        wasn't supplied at construction time). Loaded entities preserve
        their original marker on round-trip, so we only replace the sentinel.

        Nested sentinels (e.g. a DataclassEntity directly embedded inside
        another without ``Child[T]`` wrapping) survive untouched here;
        ``fxtr_cbor_dumps_with_marker_check`` rejects them with
        ``DuplicateEntityMarkerError`` at encoding time. The
        ``_fxtr_store_impl`` wrapper adds a Child[T] hint to that error.
        """
        state = RecursiveStore()
        token = RECURSIVE_SERDES_STATE.set(state)
        try:
            cbor = self.to_dict()
        finally:
            RECURSIVE_SERDES_STATE.reset(token)

        if isinstance(cbor.get("$fxtr.ctx"), RawFxtrEntityMissingMarkerSentinel):
            cbor["$fxtr.ctx"] = RawFxtrEntityMarkerID(
                inner_id=get_active_scope().get_context_marker_id()
            )
        return cbor

    @typing.override
    def _fxtr_store_impl(self) -> EntityRef[Self]:
        cbor = self._fxtr_convert_to_cbor()
        store_type = _infer_default_type_params(type(self))
        scope = get_active_scope()
        try:
            fid = scope.store_cbor(cbor)
        except DuplicateEntityMarkerError as exc:
            exc.add_note(
                "This error likely means that you have directly nested one DataclassEntity inside"
                " another DataclassEntity. This is allowed, but requires special handling, because"
                " the child entity must be replaced by a reference when serializing the parent for"
                " storage. Please wrap inline child entities with Child[T] to inform fxtr that it"
                " should serialize the child as a reference, e.g. `my_field:"
                " Child[SomeChildEntity]` instead of just `my_field: SomeChildEntity`."
            )
            raise

        return EntityRef(fid, store_type)


class DataclassFxtrSubpart(mashumaro.DataClassDictMixin):
    """Base class for parts of a DataclassEntity that need to be serialized into their parent.

    This class does not define a new entity type, but instead extends the existing mashumaro
    dataclass serialization mixin (DataClassDictMixin) to support serialization of fxtr datatypes.
    This can be used as the base class for dataclasses that are contained inside DataclassEntity
    and may contain fxtr datatypes internally, but do not need to be stored independently.
    """

    class Config(mashumaro.config.BaseConfig):
        serialization_strategy = DataclassEntity.Config.serialization_strategy


def _infer_default_type_params(cls: type) -> Any:
    """If cls is a generic class, return cls[...] with default type args inferred from bounds.

    For each type parameter:
    - No bound -> typing.Any
    - Bound is exactly Entity -> UnknownEntity
    - Otherwise -> the bound itself
    """
    type_params = getattr(cls, "__type_params__", None)
    if not type_params:
        return cls

    from fxtr2.conversion.converters.unknown_entity import UnknownEntity

    default_args: list[Any] = []
    for tp in type_params:
        bound = getattr(tp, "__bound__", None)
        if bound is None:
            default_args.append(Any)
        elif bound is Entity:
            default_args.append(UnknownEntity)
        else:
            default_args.append(bound)

    key = tuple(default_args)
    return cls[key[0]] if len(key) == 1 else cls[key]  # type: ignore[call-overload]


@functools.lru_cache(maxsize=1024)
def _build_mashumaro_decoder(ty: Any) -> Callable[[FxtrCbor], Any]:
    """Build a mashumaro BasicDecoder for the given type.

    This is used internally by DataclassEntity to deserialize with proper type parameter
    substitution. The type should be a concrete type or generic alias (e.g., WrapperWithRef[TypeA]),
    not a union.
    """
    decoder = mashumaro.codecs.BasicDecoder(ty)

    def decode(raw_entity: FxtrCbor) -> Any:
        return decoder.decode(raw_entity)

    return decode


@functools.lru_cache(maxsize=1024)
def _build_mashumaro_encoder(ty: Any) -> Callable[[Any], FxtrCbor]:
    """Build a mashumaro BasicEncoder for the given type.

    This is used internally by DataclassEntity to serialize with proper type parameter
    substitution. The type should be a concrete type or generic alias (e.g., WrapperWithRef[TypeA]),
    not a union.
    """
    encoder = mashumaro.codecs.BasicEncoder(ty)

    def encode(value: Any) -> FxtrCbor:
        return encoder.encode(value)

    return encode


@dataclasses.dataclass(frozen=True)
class DataclassEntityLoadHandler(EntityLoadHandler[DataclassEntity]):
    """Conversion handler for DataclassEntity."""

    @typing.override
    async def supports_entity_type(
        self, type_id: FxtrID, target_type: EntityTypeAnnotation[DataclassEntity]
    ) -> bool:
        base_type = typing.get_origin(target_type)
        if base_type is None:
            base_type = target_type
        if not (isinstance(base_type, type) and issubclass(base_type, DataclassEntity)):
            raise TypeError(
                f"DataclassEntityConversionHandler should never be called with a"
                f" type that is not a subclass of DataclassEntity"
            )
        if base_type is DataclassEntity:
            return False

        return type_id == base_type._fxtr_known_type_id

    @typing.override
    async def from_lowlevel_entity_data(
        self, cbor: FxtrCbor, target_type: EntityTypeAnnotation[DataclassEntity]
    ) -> DeserializedEntity[DataclassEntity]:
        """Convert from fxtr-CBOR using mashumaro with two-phase loading for inline entities.

        ``target_type`` is always a concrete type (the caller in ``entity_ref_scope`` resolves the
        stored ``$fxtr.type`` to a single concrete subtype before dispatching here). We build a
        ``BasicDecoder`` for it so type parameters are properly substituted in nested generic fields.

        If the entity has Child[T] or LinkedEntity[T] fields, this method returns an Awaitable
        that loads those entities asynchronously before returning the final entity.
        """
        assert isinstance(cbor, dict)
        # Stored cbor always has a real RawFxtrEntityMarkerID at $fxtr.ctx
        # (the encoder rejects sentinels at storage time). Reject any
        # other value here defensively, including the sentinel default
        # — sentinels indicate the cbor never went through a proper
        # store path.
        ctx = cbor.get("$fxtr.ctx")
        if not isinstance(ctx, RawFxtrEntityMarkerID):
            raise ValueError(
                "Cannot load DataclassEntity from cbor whose $fxtr.ctx is not a"
                f" RawFxtrEntityMarkerID (got {type(ctx).__name__})"
            )

        # Phase 1: Collecting - deserialize to find inline entity refs
        collecting_state = RecursiveLoadCollecting([])
        token = RECURSIVE_SERDES_STATE.set(collecting_state)
        try:
            converted = _build_mashumaro_decoder(target_type)(cbor)
        finally:
            RECURSIVE_SERDES_STATE.reset(token)

        collected = collecting_state.collected
        if collected:
            # Load all collected refs
            entities = [await ref.load() for ref, _ in collected]

            # Phase 2: Rebuilding with loaded entities
            rebuild_state = RecursiveLoadRebuilding(entities, collected, 0)
            rebuild_token = RECURSIVE_SERDES_STATE.set(rebuild_state)
            try:
                converted = _build_mashumaro_decoder(target_type)(cbor)
            finally:
                RECURSIVE_SERDES_STATE.reset(rebuild_token)

        try:
            hash(converted)
            is_immutable = True
        except Exception:
            is_immutable = False
        return DeserializedEntity(value=converted, cacheable=is_immutable)


register_entity_load_handler(DataclassEntity, DataclassEntityLoadHandler())
