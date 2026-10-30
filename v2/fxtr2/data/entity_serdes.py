"""High-level serialization and deserialization for fxtr entities.

This module provides a user-friendly API for working with fxtr entities using mashumaro
(a flexible Python serialization library). It sits on top of the low-level CBOR encoding
in entity_core.py.

Key components:
- Entity: Abstract base class for serializable entities
- DataclassEntity: Convenient base for dataclass-based entities with automatic type discrimination
- EntityRef[T]: A lazy reference to an entity that can be loaded on demand
- OutputStream: Experimental streaming output interface

Typical usage:
    @dataclass(frozen=True)
    class MyEntity(DataclassEntity, fxtr_type=system_entities.define_type("my_type")):
        name: str
        data: list[int]

    # Store an entity
    ref = MyEntity(name="test", data=[1, 2, 3]).store()

    # Load it back
    entity = await ref.load()

This module attempts to always preserve the _full_ annotated type of a given instance even through
loads and stores, so that generic types can be losslessly converted between an Entity and an
EntityRef.
"""

from __future__ import annotations

import abc
import contextvars
import dataclasses
import functools
import inspect
import logging
import typing
import weakref
from typing import Annotated, Any, Awaitable, Callable, Generic, NoReturn, Self

import anyio
import mashumaro
import mashumaro.codecs
import mashumaro.config
import mashumaro.helper
import mashumaro.types
from frozendict import frozendict

from fxtr2.data.abstract_arena import StreamSink
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)

from . import fxtr_scope

logger = logging.getLogger(__name__)


class Entity(abc.ABC):
    """Base class for python objects that should be serialized as entities in the fxtr graph.

    Fxtr will use this class to serialize and deserialize entities to/from the fxtr graph. The
    main restrictions are:
    - Entities that compare equal must have the same fxtr-CBOR representation, and vice versa.
    - Loading an entity and then storing that entity must produce the same fxtr-CBOR representation.
    - Usually, storing an entity and then loading it should produce the same entity object. (In some
      cases some information can be lost, e.g. tuples may be converted to lists, but this should be
      avoided where possible.)
    - The serialized form must have exactly one RawFxtrEntityMarkerID, located at "$fxtr.ctx" at
      the root of the dictionary.
    - Each entity class must have a hardcoded type ID, and every serialized entity of that type must
      have that type ID.
    - If _fxtr_immutable() returns True, the entity must be actually immutable, and it will be
      cached and reused automatically across fxtr boundaries.

    Serialization and deserialization methods (_to_fxtr_cbor and _from_fxtr_cbor) may store or load
    child entities from the active scope (ACTIVE_SCOPE). The caller guarantees that ACTIVE_SCOPE is
    set to the correct scope before calling these methods.
    """

    @abc.abstractmethod
    def _to_fxtr_cbor(self) -> FxtrCbor:
        """Convert the entity to its fxtr-CBOR representation.

        This method may store child entities into the active scope (ACTIVE_SCOPE.get()) as part of
        serialization. The caller guarantees that ACTIVE_SCOPE is set to the scope being stored into.
        """
        raise NotImplementedError("Not implemented")

    @classmethod
    @abc.abstractmethod
    def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> Self | Awaitable[Self]:
        """Convert the entity from its fxtr-CBOR representation.

        This method may load child entities from the active scope (ACTIVE_SCOPE.get()) as part of
        deserialization. The caller guarantees that ACTIVE_SCOPE is set to the scope being loaded
        from.

        If async operations are needed (e.g., to load child entities), this method may return an
        Awaitable[Self] instead of Self. Callers must check with inspect.isawaitable() and await
        if needed.

        Args:
            cbor: The fxtr-CBOR representation of the entity.
            annotated_type: The annotated type of the entity. When available, this is the type that
                was used to store the entity; this can be used to deserialize the entity to the
                correct type.

        Returns:
            The entity, or an Awaitable that resolves to the entity.
        """
        raise NotImplementedError("Not implemented")

    def _fxtr_immutable(self) -> bool:
        """Whether the entity promises to be immutable. If so, it may be cached."""
        return False

    @classmethod
    @abc.abstractmethod
    def _fxtr_type_id(cls) -> FxtrID:
        """Get the type ID for the entity."""
        raise NotImplementedError("Not implemented")

    def _fxtr_notify_loaded_from_ref(self, ref: EntityRef) -> None:
        """Notify the entity that it was loaded from a reference.

        This can be used for immutable entities to track that they already have a reference, which
        they can use for caching if applicable.

        Args:
            ref: A reference that was used to load the entity. Can be used for caching if
                applicable.
        """


@dataclasses.dataclass(frozen=True)
class FxtrIDChildStrengthTag:
    """Annotation tag for indicating the strength of a linked FxtrID."""

    strong: bool


# Type aliases for FxtrID link strength
type ChildFxtrID = Annotated[FxtrID, FxtrIDChildStrengthTag(strong=True)]
"""An FxtrID that represents a strong (ownership) link to another entity."""


class EntityNotLoadedError(RuntimeError):
    """Error raised when attempting to access an entity via an EntityRef that is safely loaded.

    This error is raised when accessing the .val of an EntityRef when either (a) the value has not
    yet been cached, or (b) the cached value is unsafe to use due to concurrent changes to the
    arena. In either case, the entity should be asynchronously loaded via .load() instead.
    """

    def __init__(self, ref: EntityRef, message: str):
        self.ref = ref
        super().__init__(message)


def _get_entity_base_type(ty: Any) -> type[Entity] | None:
    """Get the base Entity class from a type, handling generic aliases.

    Returns the base Entity class if ty is an Entity subclass or a generic alias of one,
    otherwise returns None.
    """
    origin = typing.get_origin(ty)
    base = origin if origin is not None else ty
    if isinstance(base, type) and issubclass(base, Entity):
        return base
    return None


def _is_union_type(ty: Any) -> bool:
    """Check if a type is a union (X | Y or Union[X, Y])."""
    import types

    origin = typing.get_origin(ty)
    return origin is types.UnionType or origin is typing.Union


@functools.lru_cache(maxsize=1024)
def _build_mashumaro_converter(ty: Any) -> Callable[[FxtrCbor], Any]:
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
def build_converter_for(ty: Any) -> Callable[[FxtrCbor, fxtr_scope.FxtrScope], Awaitable[Any]]:
    """Builds an async converter function for an entity type.

    Args:
        ty: The type to build a converter for. Can be:
            - An Entity subclass (e.g., MyEntity)
            - A generic alias of an Entity (e.g., MyEntity[TypeA])
            - A union of Entity types (e.g., MyEntity | OtherEntity)

    Returns:
        An async function that converts FxtrCbor to an entity of the given type.
        The converter takes a scope parameter and ensures ACTIVE_SCOPE is set correctly
        before calling _from_fxtr_cbor. It handles both sync and async _from_fxtr_cbor
        implementations.

    For unions, the converter inspects the $fxtr.type field to dispatch to the correct variant.
    For single Entity types, it calls _from_fxtr_cbor with the annotated type so that type
    parameters can be used during deserialization.
    """
    # Handle unions: dispatch based on $fxtr.type discriminator
    if _is_union_type(ty):
        union_args = typing.get_args(ty)

        # Build a mapping from type_id -> (annotated_type, base_class)
        # We need to handle both concrete types and generic aliases in the union
        type_id_to_info: dict[FxtrID, tuple[Any, type[Entity]]] = {}

        for arg in union_args:
            base = _get_entity_base_type(arg)
            if base is None:
                raise ValueError(
                    f"Union member {arg} is not an Entity type. "
                    "build_converter_for only supports unions of Entity types."
                )
            type_id = base._fxtr_type_id()
            if type_id in type_id_to_info:
                raise ValueError(
                    f"Multiple union members have the same type ID {type_id}. Cannot disambiguate."
                )
            type_id_to_info[type_id] = (arg, base)

        async def convert_union(cbor: FxtrCbor, scope: fxtr_scope.FxtrScope) -> Any:
            assert isinstance(cbor, dict), f"Expected dict, got {type(cbor)}"
            type_field = cbor.get("$fxtr.type")
            if type_field is None:
                raise ValueError("Missing $fxtr.type field in entity CBOR")

            # Extract the FxtrID from the raw wrapper
            if isinstance(type_field, (RawWeakFxtrID, RawStrongFxtrID)):
                type_id = type_field.inner_id
            else:
                raise ValueError(f"Unexpected $fxtr.type value: {type_field}")

            info = type_id_to_info.get(type_id)
            if info is None:
                raise ValueError(
                    f"Unknown type ID {type_id} not in union {ty}. "
                    f"Known IDs: {list(type_id_to_info.keys())}"
                )

            annotated_type, base_class = info
            # Override ACTIVE_SCOPE for the deserialization
            token = fxtr_scope.ACTIVE_SCOPE.set(scope)
            try:
                result = base_class._from_fxtr_cbor(cbor, annotated_type)
                if inspect.isawaitable(result):
                    return await result
                return result
            finally:
                fxtr_scope.ACTIVE_SCOPE.reset(token)

        return convert_union

    # Handle single Entity type (possibly a generic alias)
    base = _get_entity_base_type(ty)
    if base is not None:

        async def convert_entity(cbor: FxtrCbor, scope: fxtr_scope.FxtrScope) -> Any:
            # Override ACTIVE_SCOPE for the deserialization
            token = fxtr_scope.ACTIVE_SCOPE.set(scope)
            try:
                result = base._from_fxtr_cbor(cbor, ty)
                if inspect.isawaitable(result):
                    return await result
                return result
            finally:
                fxtr_scope.ACTIVE_SCOPE.reset(token)

        return convert_entity

    raise TypeError(
        f"Cannot build a converter for type {ty}: it is not a subclass of Entity or a union of"
        " Entity subclasses."
    )


T_co = typing.TypeVar("T_co", bound=Entity, covariant=True)


class EntityRef(Generic[T_co], mashumaro.types.GenericSerializableType):
    """A reference to an entity in the fxtr graph."""

    ref: FxtrID
    converter: Callable[[FxtrCbor, fxtr_scope.FxtrScope], Awaitable[T_co]]
    _cached_entity: T_co | None = dataclasses.field(default=None, repr=False, compare=False)

    def __init__(
        self, ref: FxtrID, converter: Callable[[FxtrCbor, fxtr_scope.FxtrScope], Awaitable[T_co]]
    ):
        super().__setattr__("ref", ref)
        super().__setattr__("converter", converter)
        super().__setattr__("_cached_entity", None)

    def __eq__(self, other: Any) -> bool:
        return (
            type(other) is type(self)
            and self.ref == other.ref
            and self.converter == other.converter
        )

    def __hash__(self) -> int:
        return hash((type(self), self.ref, self.converter))

    def __setattr__(self, name: str, value: Any) -> None:
        if name != "__orig_class__":
            raise AttributeError("EntityRef is immutable")
        super().__setattr__(name, value)

    @property
    def val(self) -> T_co:
        if self._cached_entity is not None:
            scope = fxtr_scope.ACTIVE_SCOPE.get()
            if scope.can_use_cached_load(self.ref):
                # We have a cached entity and have been notified by the arena that it is safe to
                # use.
                return self._cached_entity
            else:
                raise EntityNotLoadedError(
                    self,
                    "This EntityRef is not safe to use synchronously in the current arena! Please"
                    " either use EntityRef.load() to load it asynchronously, or use the helper"
                    " `deep_load`.",
                )
        else:
            raise EntityNotLoadedError(
                self,
                "EntityRef is not cached, so its value cannot be accessed synchronously! Please"
                " either use EntityRef.load() to load it asynchronously, or use the helper"
                " `deep_load`.",
            )

    async def load(self, scope: fxtr_scope.FxtrScope | None = None) -> T_co:
        if scope is None:
            scope = fxtr_scope.ACTIVE_SCOPE.get()

        if self._cached_entity is not None and scope.can_use_cached_load(self.ref):
            # Safe to use a cached entity.
            logger.debug(f"%s using cached entity", self)
            return self._cached_entity
        else:
            # We need to load the entity again.
            logger.debug(f"%s loading entity from arena", self)
            loaded = await scope.load_cbor(self.ref)
            if self._cached_entity is not None:
                # We already have a cached entity, so we don't even need to bother converting the
                # loaded data, we can just use the cached one. (If we got here, it was because the
                # arena told us it was unsafe to use the cached entity without the async load step,
                # but the entity itself should still be immutable.)
                logger.debug(f"%s using cached entity after load check", self)
                return self._cached_entity
            # Otherwise, we don't have a cached entity, so we need to convert the loaded data.
            # The converter is async and will set ACTIVE_SCOPE appropriately.
            entity = await self.converter(loaded, scope)
            if entity._fxtr_immutable():
                # Entity declares itself to be immutable, so we can cache the result.
                super().__setattr__("_cached_entity", entity)
                entity._fxtr_notify_loaded_from_ref(self)
                logger.debug(f"%s caching loaded entity", self)
            return entity

    def _unsafe_set_cached_entity(self, entity: Entity) -> None:
        """Set the cached entity.

        This is an unsafe method and should only be called by someone who just built the reference
        and knows what it converts to.
        """
        super().__setattr__("_cached_entity", entity)

    def clear_cache(self) -> Self:
        """Clear the cached entity, if any. Returns self for method chaining convenience."""
        super().__setattr__("_cached_entity", None)
        return self

    def __repr__(self):
        if hasattr(self, "__orig_class__"):
            [ty] = typing.get_args(self.__orig_class__)  # type: ignore
            if isinstance(ty, type):
                ty = ty.__qualname__
            return f"<EntityRef[{ty}] {self.ref.summary()}>"
        return f"<EntityRef {self.ref.summary()}>"

    async def copy_to(
        self, scope: fxtr_scope.FxtrScope, *, from_scope: fxtr_scope.FxtrScope | None = None
    ) -> Self:
        """Copy the entity to another scope (usually from the active scope).

        Args:
            scope: The scope to copy the entity to.
            from_scope: Optional, the scope to copy the entity from. Defaults to the active scope.

        Returns:
            The ref itself, for convenient chaining.
        """
        if from_scope is None:
            from_scope = fxtr_scope.ACTIVE_SCOPE.get()

        return await scope.copy_from_scope(self, from_scope)

    async def copy_from(
        self, scope: fxtr_scope.FxtrScope, *, to_scope: fxtr_scope.FxtrScope | None = None
    ) -> Self:
        """Copy the entity from another scope (usually the active scope).

        Args:
            scope: The scope to copy the entity to.
            to_scope: Optional, the scope to copy the entity to. Defaults to the active scope.

        Returns:
            The ref itself, for convenient chaining.
        """
        if to_scope is None:
            to_scope = fxtr_scope.ACTIVE_SCOPE.get()

        return await to_scope.copy_from_scope(self, scope)

    def lift(self, scope: fxtr_scope.FxtrScope | None = None) -> Self:
        """Lift the entity to a given scope (usually the active scope).

        Args:
            scope: The scope to lift the entity to. Defaults to the active scope.

        Returns:
            The ref itself, for convenient chaining.
        """
        if scope is None:
            scope = fxtr_scope.ACTIVE_SCOPE.get()

        return scope.lift(self)


async def deep_load[T](thunk: Callable[[], T], scope: fxtr_scope.FxtrScope | None = None) -> T:
    """Load all EntityRefs whose .val are accessed by a thunk, calling it repeatedly if necessary.

    This is a convenience function for accessing entities via the EntityRef.val property, when
    it is possible that some of the EntityRefs needed are not yet loaded. It repeatedly calls the
    thunk, and loads any EntityRefs whose value is accessed while the thunk is executing.

    Args:
        thunk: A thunk that returns the value to access.
        scope: The scope to use for loading the entities.

    Returns:
        The value returned by the thunk.
    """
    if scope is None:
        scope = fxtr_scope.ACTIVE_SCOPE.get()

    loaded_ents: set[EntityRef] = set()

    # Override the active scope for the duration of the deep_load operation.
    scope_override_token = fxtr_scope.ACTIVE_SCOPE.set(scope)
    try:
        while True:
            try:
                return thunk()
            except EntityNotLoadedError as e:
                if e.ref in loaded_ents:
                    raise RuntimeError(
                        f"EntityRef {e.ref} was loaded, but the results of the load were not"
                        " cached! Cannot use deep_load with this EntityRef. This can happen when"
                        " an entity is either itself mutable or contains mutable elements."
                    ) from e
                logger.info(f"deep_load triggering load of {e.ref}")
                await e.ref.load()
                loaded_ents.add(e.ref)
    finally:
        fxtr_scope.ACTIVE_SCOPE.reset(scope_override_token)


# Type aliases for EntityRef link strength
type ChildRef[T: Entity] = Annotated[EntityRef[T], FxtrIDChildStrengthTag(strong=True)]
"""An EntityRef that represents a strong (ownership) link to another entity."""

# Backwards compatibility aliases (deprecated)
type StrongLinkedEntityRef[T: Entity] = ChildRef[T]
"""Deprecated: Use ChildRef instead."""


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

    scope: fxtr_scope.FxtrScope
    collected: list[tuple[EntityRef[Any], Any]]
    """List of (entity_ref, type_annotation) pairs collected during deserialization."""


@dataclasses.dataclass
class RecursiveLoadRebuilding:
    """State during phase 2 (rebuilding) of two-phase deserialization.

    During this phase, inline entity fields return the pre-loaded entities
    instead of placeholders. The collected list is kept for validation.
    """

    scope: fxtr_scope.FxtrScope
    entities: list[Entity]
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

    scope: fxtr_scope.FxtrScope


# Context variable for tracking recursive serialization/deserialization state
RECURSIVE_SERDES_STATE: contextvars.ContextVar[
    RecursiveLoadCollecting | RecursiveLoadRebuilding | RecursiveStore | None
] = contextvars.ContextVar("recursive_serdes_state", default=None)


# Type aliases for inline entities (auto-loaded during deserialization)
@dataclasses.dataclass(frozen=True)
class _InlineEntityMarker:
    """Marker annotation for inline entity types."""

    strong: bool


type Child[T: Entity] = Annotated[T, _InlineEntityMarker(strong=True)]
"""An inline entity with a strong (ownership) link. Auto-loaded during deserialization."""

type LinkedEntity[T: Entity] = Annotated[T, _InlineEntityMarker(strong=False)]
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

    fxtr_id: FxtrID
    target_type: type[_T_InlineEntity] | None
    strong: bool

    def _serialize(self, types: list[type[_T_InlineEntity]]) -> RawStrongFxtrID | RawWeakFxtrID:
        assert len(types) == 1
        if self.strong:
            return RawStrongFxtrID(inner_id=self.fxtr_id)
        else:
            return RawWeakFxtrID(inner_id=self.fxtr_id)

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
        state = RECURSIVE_SERDES_STATE.get()
        if not isinstance(state, RecursiveStore):
            raise RuntimeError(
                "Cannot serialize inline entity outside of DataclassEntity serialization context. "
                "Make sure you're using DataclassEntity._to_fxtr_cbor() or store() method."
            )

        scope = state.scope

        # Temporarily reset state before storing child to get fresh context
        token = RECURSIVE_SERDES_STATE.set(None)
        try:
            ref = store_entity(value, scope=scope)
        finally:
            RECURSIVE_SERDES_STATE.reset(token)

        return _InlineEntitySerializationHelper(
            fxtr_id=ref.ref, target_type=None, strong=self.strong
        )

    def deserialize(
        self, value: _InlineEntitySerializationHelper[_T_InlineEntity]
    ) -> _T_InlineEntity | DeferredLoadPlaceholder:
        """Deserialize by collecting ref (phase 1) or returning loaded entity (phase 2)."""
        fxtr_id = value.fxtr_id
        target_type = value.target_type
        assert (
            target_type is not None
        ), "target_type should be set by _InlineEntitySerializationHelper._deserialize"

        state = RECURSIVE_SERDES_STATE.get()

        if isinstance(state, RecursiveLoadCollecting):
            # Phase 1: collect the ref and return a placeholder
            converter = build_converter_for(target_type)
            ref = EntityRef(fxtr_id, converter)
            index = len(state.collected)
            state.collected.append((ref, target_type))
            return DeferredLoadPlaceholder(index=index)

        elif isinstance(state, RecursiveLoadRebuilding):
            # Phase 2: return the loaded entity with validation
            index = state.next_index
            state.next_index += 1

            # Validate that we're returning the same ref we collected
            collected_ref, collected_type = state.collected[index]
            if collected_ref.ref != fxtr_id:
                raise RuntimeError(
                    f"Deserialization order mismatch at index {index}: "
                    f"collected {collected_ref.ref} but rebuilding got {fxtr_id}"
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
class _EntityRefSerializationHelper[T: Entity](mashumaro.types.GenericSerializableType):
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
        helper = _EntityRefSerializationHelper(ref=value.ref, target_type=None, strong=self.strong)
        return helper

    def deserialize(
        self, value: _EntityRefSerializationHelper[_T_ERefSerStrat]
    ) -> EntityRef[_T_ERefSerStrat]:
        assert self.strong == value.strong
        assert value.target_type is not None
        converter = build_converter_for(value.target_type)
        return EntityRef(ref=value.ref, converter=converter)


# =============================================================================
# Collection Serialization Strategies (frozendict, set, frozenset)
# =============================================================================

from frozendict import frozendict


def _canonical_set_sort_key(item: Any) -> tuple[int, Any]:
    """Return a sort key for canonical ordering of set/frozenset elements."""
    if isinstance(item, FxtrID):
        return (0, item._raw_data_id)
    elif isinstance(item, EntityRef):
        return (1, item.ref._raw_data_id)
    elif isinstance(item, str):
        return (2, item)
    elif isinstance(item, bytes):
        return (3, item)
    elif isinstance(item, int):
        return (4, item)
    else:
        raise ValueError(
            "Default set canonicalization order only supports string, int, bytes, FxtrID, and"
            f" EntityRef items, but got {type(item)}. If you need canonical serialization of other"
            " types, you should override set serialization."
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
        return sorted(value, key=_canonical_set_sort_key)

    def deserialize(self, value: list[_T_m1]) -> set[_T_m1]:
        return set(value)


class FrozensetSerializationStrategy(mashumaro.types.SerializationStrategy, Generic[_T_m1]):
    """Frozenset serialization with canonical ordering."""

    def serialize(self, value: frozenset[_T_m1]) -> list[_T_m1]:
        return sorted(value, key=_canonical_set_sort_key)

    def deserialize(self, value: list[_T_m1]) -> frozenset[_T_m1]:
        return frozenset(value)


def _bad_fxtr_type() -> NoReturn:
    raise ValueError(
        "No value was provided for fxtr_type in a DataclassEntity subclass! Usually a default value"
        " should be provided either in the class definition or as a class keyword argument."
    )


def preserve_orig_class[T](cls: T) -> T:
    """Decorator that modifies a frozen dataclass to allow __orig_class__ to be set.

    Python's generic instantiation (e.g., Foo[Bar](...)) sets __orig_class__ on the instance,
    but frozen dataclasses block all attribute setting. This decorator wraps the frozen
    __setattr__ to allow __orig_class__ while preserving immutability for everything else.

    Apply this decorator AFTER @dataclasses.dataclass(frozen=True) on any DataclassEntity
    subclass that uses type parameters and needs to preserve them through store/load cycles.

    Example:
        @preserve_orig_class
        @dataclasses.dataclass(frozen=True)
        class MyGenericEntity[T](DataclassEntity, fxtr_type=...):
            item: T
    """
    original_setattr = cls.__setattr__

    def new_setattr(self, name: str, value: Any) -> None:
        if name == "__orig_class__":
            object.__setattr__(self, name, value)
        else:
            original_setattr(self, name, value)  # type: ignore # Will raise FrozenInstanceError

    cls.__setattr__ = new_setattr  # type: ignore
    return cls


@preserve_orig_class
@dataclasses.dataclass(frozen=True, kw_only=True)
class DataclassEntity(Entity, mashumaro.DataClassDictMixin):
    """Base class for entities that are represented as mashumaro-serializable dataclasses.

    Mashumaro provides strong support for extensible serialization and deserialization. This base
    class provides a convenient way to create entities that are represented as
    mashumaro-serializable dataclasses. It also configures instances of itself to automatically
    use the correct fields for tagged decoding.

    Subclasses must set the fxtr_type class attribute to the FxtrID of the entity type.
    """

    fxtr_type: typing.Final[FxtrID] = dataclasses.field(
        default_factory=_bad_fxtr_type, repr=False, kw_only=True
    )
    fxtr_ctx: typing.Final[RawFxtrEntityMarkerID] = dataclasses.field(
        default_factory=fxtr_scope.get_active_context_marker,
        compare=False,
        repr=False,
        kw_only=True,
    )

    class Config(mashumaro.config.BaseConfig):
        aliases = {"fxtr_type": "$fxtr.type", "fxtr_ctx": "$fxtr.ctx"}
        serialize_by_alias = True
        discriminator = mashumaro.types.Discriminator(
            field="$fxtr.type",
            include_subtypes=True,
            variant_tagger_fn=lambda cls: cls._fxtr_known_type_id,
        )
        serialization_strategy = {
            # FxtrID: weak by default, ChildFxtrID for strong links
            FxtrID: FxtrIDSerializationStrategy(strong=False),
            ChildFxtrID: FxtrIDSerializationStrategy(strong=True),
            # EntityRef: weak by default, ChildRef for strong links
            EntityRef: EntityRefSerializationStrategy(strong=False),
            ChildRef: EntityRefSerializationStrategy(strong=True),
            # Backwards compatibility alias
            StrongLinkedEntityRef: EntityRefSerializationStrategy(strong=True),
            # Inline entities: auto-loaded during deserialization
            Child: InlineEntitySerializationStrategy(strong=True),
            LinkedEntity: InlineEntitySerializationStrategy(strong=False),
            # Pass through for context markers
            RawFxtrEntityMarkerID: mashumaro.helper.pass_through,
            # Collection types
            frozendict: FrozendictSerializationStrategy(),
            set: SetSerializationStrategy(),
            frozenset: FrozensetSerializationStrategy(),
        }

    def _to_fxtr_cbor(self) -> FxtrCbor:
        # Set up RecursiveStore context for inline entity serialization
        scope = fxtr_scope.ACTIVE_SCOPE.get()
        state = RecursiveStore(scope)
        token = RECURSIVE_SERDES_STATE.set(state)
        try:
            return self.to_dict()
        finally:
            RECURSIVE_SERDES_STATE.reset(token)

    @classmethod
    def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> Self | Awaitable[Self]:
        """Convert from fxtr-CBOR using mashumaro with two-phase loading for inline entities.

        If annotated_type is provided (e.g., WrapperWithRef[TypeA]), we use a BasicDecoder
        for that type so that type parameters are properly substituted in nested generic fields.
        Otherwise, we fall back to cls.from_dict() which uses mashumaro's discriminator support.

        If the entity has Child[T] or LinkedEntity[T] fields, this method returns an Awaitable
        that loads those entities asynchronously before returning the final entity.
        """
        assert isinstance(cbor, dict)
        scope = fxtr_scope.ACTIVE_SCOPE.get()

        # Phase 1: Collecting - deserialize to find inline entity refs
        collecting_state = RecursiveLoadCollecting(scope, [])
        token = RECURSIVE_SERDES_STATE.set(collecting_state)
        try:
            if annotated_type is not None:
                _obj = _build_mashumaro_converter(annotated_type)(cbor)
            else:
                _obj = cls.from_dict(cbor)
            collected = collecting_state.collected[:]  # Copy before reset
        finally:
            RECURSIVE_SERDES_STATE.reset(token)

        if not collected:
            # No inline entities to load - return synchronously
            return _obj  # type: ignore[return-value]

        # Has inline entities - return async loader
        async def load_and_rebuild() -> Self:
            # Load all collected refs in parallel
            entities: list[Entity] = [None] * len(collected)  # type: ignore[list-item]

            async def load_one(i: int, ref: EntityRef[Any]) -> None:
                entities[i] = await ref.load(scope)

            async with anyio.create_task_group() as tg:
                for i, (ref, _type_ann) in enumerate(collected):
                    tg.start_soon(load_one, i, ref)

            # Phase 2: Rebuilding with loaded entities
            rebuild_state = RecursiveLoadRebuilding(scope, entities, collected, 0)
            rebuild_token = RECURSIVE_SERDES_STATE.set(rebuild_state)
            try:
                if annotated_type is not None:
                    return _build_mashumaro_converter(annotated_type)(cbor)
                else:
                    return cls.from_dict(cbor)
            finally:
                RECURSIVE_SERDES_STATE.reset(rebuild_token)

        return load_and_rebuild()

    @classmethod
    def _fxtr_type_id(cls) -> FxtrID:
        assert cls._fxtr_known_type_id is not None
        return cls._fxtr_known_type_id

    def _fxtr_immutable(self) -> bool:
        try:
            hash(self)
            return True
        except Exception:
            return False

    def _fxtr_notify_loaded_from_ref(self, ref: EntityRef) -> None:
        # Cache the ref so that if we store this entity again, we can use the cached ref. This
        # is possible because it is a requirement that loading an entity and then storing it again
        # produces the same serialized form. (The reverse is not always true.)
        if self._fxtr_immutable():
            super().__setattr__("_fxtr_cached_ref_weakref", weakref.ref(ref))

    def __init_subclass__(cls, fxtr_type: FxtrID | None = None, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if fxtr_type is not None:
            if "fxtr_type" in cls.__annotations__ or "fxtr_type" in cls.__dict__:
                raise ValueError("Cannot set fxtr_type as class keyword argument and also field!")
            cls.__annotations__["fxtr_type"] = typing.Final[FxtrID]
            cls.fxtr_type = dataclasses.field(default=fxtr_type, repr=False, kw_only=True)  # type: ignore
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

        # Save a weak reference to the EntityRef that this entity gets stored as.
        super().__setattr__("_fxtr_cached_ref_weakref", None)

    def store(self, scope: fxtr_scope.FxtrScope | None = None) -> EntityRef[Self]:
        """Store the entity in the current arena and return a reference to it."""
        if scope is None:
            scope = fxtr_scope.ACTIVE_SCOPE.get()

        fxtr_cached_ref_weakref: weakref.WeakRef[EntityRef[Self]] | None = self._fxtr_cached_ref_weakref  # type: ignore
        if fxtr_cached_ref_weakref is not None:
            fxtr_cached_ref = fxtr_cached_ref_weakref()
        else:
            fxtr_cached_ref = None

        self_immutable = self._fxtr_immutable()
        if fxtr_cached_ref is not None and self_immutable:
            # Track a cached store where we already know the ID.
            # Override ACTIVE_SCOPE so nested _to_fxtr_cbor calls use the correct scope.
            def _serialize_with_scope() -> FxtrCbor:
                token = fxtr_scope.ACTIVE_SCOPE.set(scope)
                try:
                    return self._to_fxtr_cbor()
                finally:
                    fxtr_scope.ACTIVE_SCOPE.reset(token)

            scope.track_cached_store_cbor(_serialize_with_scope, cached_id=fxtr_cached_ref.ref)
            ref = fxtr_cached_ref
        else:
            # Override ACTIVE_SCOPE so nested _to_fxtr_cbor calls use the correct scope.
            token = fxtr_scope.ACTIVE_SCOPE.set(scope)
            try:
                my_cbor = self._to_fxtr_cbor()
            finally:
                fxtr_scope.ACTIVE_SCOPE.reset(token)
            my_cid = scope.store_cbor(my_cbor)
            try:
                orig_class = getattr(self, "__orig_class__")
            except AttributeError:
                orig_class = type(self)
            converter = build_converter_for(orig_class)
            ref = EntityRef(my_cid, converter)
            if self_immutable:
                # Cache the store operation, so that if we store this entity again, we re-use the
                # same ref.
                object.__setattr__(self, "_fxtr_cached_ref_weakref", weakref.ref(ref))
                # IMPORTANT: We previously set this as the cached entity on the ref, which would
                # mean we could also immediately use the result of a _load_ operation. However, this
                # does not always work consistently, because deserializing a serialized entity may
                # result in a slightly different Python representation (e.g. if you serialize a
                # value with Any). For simplicity, to avoid this we don't cache on store; we
                # require it to be re-loaded the first time the ref is expanded. This loaded
                # version will then be cached. This means that most entities that are stored and
                # loaded will have at most two copies in memory.
        return ref


def store_entity[
    T: Entity
](
    entity: T, as_type: type[T] | None = None, scope: fxtr_scope.FxtrScope | None = None
) -> EntityRef[T]:
    """Store an entity in the current arena and return its ID."""
    if scope is None:
        scope = fxtr_scope.ACTIVE_SCOPE.get()

    if isinstance(entity, DataclassEntity):
        # Special case that we can optimize with caching.
        res = entity.store(scope)
        if as_type is not None:
            # Rewrite the converter to the explicit type.
            return EntityRef(res.ref, build_converter_for(as_type))
        return res  # type: ignore[return-value]

    # Override ACTIVE_SCOPE so nested _to_fxtr_cbor calls use the correct scope.
    token = fxtr_scope.ACTIVE_SCOPE.set(scope)
    try:
        cbor_value = entity._to_fxtr_cbor()
    finally:
        fxtr_scope.ACTIVE_SCOPE.reset(token)
    ent_id = scope.store_cbor(cbor_value)
    if as_type is None:
        try:
            as_type = getattr(entity, "__orig_class__")
        except AttributeError:
            pass
        if as_type is None:
            as_type = type(entity)
    converter = build_converter_for(as_type)
    return EntityRef(ent_id, converter)


type FxtrSendable = (
    None
    | int
    | float
    | bool
    | str
    | bytes
    | tuple[FxtrSendable, ...]
    | list[FxtrSendable]
    | dict[str, FxtrSendable]
    | FxtrID
    | EntityRef[Any]
    | Entity
)
"""Types that can be serialized to fxtr-CBOR automatically."""


def visit_ids_in(
    value: FxtrSendable | FxtrCbor,
    callback: Callable[[FxtrID], Any],
) -> None:
    """Visit all IDs in a sendable or cbor value."""
    match value:
        case None | int() | float() | bool() | str() | bytes():
            pass
        case list() | tuple():
            for item in value:
                visit_ids_in(item, callback)
        case dict():
            for k, v in value.items():
                visit_ids_in(k, callback)
                visit_ids_in(v, callback)
        case FxtrID():
            callback(value)
        case EntityRef():
            callback(value.ref)
        case RawWeakFxtrID() | RawStrongFxtrID() | RawFxtrEntityMarkerID():
            callback(value.inner_id)
        case Entity():
            cbor_value = value._to_fxtr_cbor()
            visit_ids_in(cbor_value, callback)


async def async_visit_ids_in(
    value: FxtrSendable | FxtrCbor, callback: Callable[[FxtrID], Awaitable[Any]]
) -> None:
    """Async version: Visit all IDs in a sendable or cbor value."""
    match value:
        case None | int() | float() | bool() | str() | bytes():
            pass
        case list() | tuple():
            async with anyio.create_task_group() as tg:
                for item in value:
                    tg.start_soon(lambda item=item: async_visit_ids_in(item, callback))
        case dict():
            async with anyio.create_task_group() as tg:
                for k, v in value.items():
                    tg.start_soon(lambda k=k: async_visit_ids_in(k, callback))
                    tg.start_soon(lambda v=v: async_visit_ids_in(v, callback))
        case FxtrID():
            await callback(value)
        case EntityRef():
            await callback(value.ref)
        case RawWeakFxtrID() | RawStrongFxtrID() | RawFxtrEntityMarkerID():
            await callback(value.inner_id)
        case Entity():
            cbor_value = value._to_fxtr_cbor()
            await async_visit_ids_in(cbor_value, callback)


def sendable_to_cbor(value: FxtrSendable) -> FxtrCbor:
    """Convert a sendable value to fxtr-CBOR."""
    match value:
        case None | int() | float() | bool() | str() | bytes():
            return value
        case list() | tuple():
            return [sendable_to_cbor(item) for item in value]
        case dict():
            return {k: sendable_to_cbor(v) for k, v in value.items()}
        case FxtrID():
            return RawWeakFxtrID(inner_id=value)
        case EntityRef():
            return RawWeakFxtrID(inner_id=value.ref)
        case Entity():
            # General case.
            ent_ref = store_entity(value)
            return RawStrongFxtrID(inner_id=ent_ref.ref)
        case _:
            raise ValueError(f"Cannot send {type(value)} with sendable_to_cbor!")


STREAM_OUTPUTS_TYPE_ID = FxtrID.builtin("StreamOutputs^1")


class OutputStream:
    """An output stream for fxtr entities and state updates.

    All entities passed to the stream will be taken from the active arena (at the time the values
    are emitted).
    """

    @dataclasses.dataclass(frozen=True)
    class _FinishAutomaticSentinel:
        pass

    FINISH_AUTOMATICALLY = _FinishAutomaticSentinel()

    def __init__(self, stream_sink: StreamSink, stream_key: FxtrID | None = None):
        self._stream_sink = stream_sink
        self._stream_key = stream_key
        self._emitted: list[FxtrSendable | OutputStream] = []
        self._final_result: FxtrID | None = None

    def emit(self, *items: FxtrSendable) -> None:
        """Emit items to the stream."""
        scope = fxtr_scope.ACTIVE_SCOPE.get()
        processed_items = sendable_to_cbor(items)
        assert isinstance(processed_items, list)
        # TODO(daniel): awkward API
        self._stream_sink.emit(*processed_items, from_arena=scope._main_arena)
        self._emitted.extend(items)

    def is_finished(self) -> bool:
        """Check if the stream is finished."""
        return self._stream_sink.is_finished()

    def finish(
        self,
        final: FxtrID | EntityRef | Entity | _FinishAutomaticSentinel = FINISH_AUTOMATICALLY,
    ) -> None:
        """Close the stream with a final result.

        After a stream is closed, it is no longer valid to emit any more items to the stream.
        Additionally, all substreams of this stream must be finished first before this stream can
        be finished.

        Args:
            final: The final result to use as the result of the stream, replacing all previous
                streamed outputs. Usually an entity or set of entities that combine the results
                that were previously streamed. If not provided (FINISH_AUTOMATICALLY), the result
                will be the list of all previously streamed outputs.
        """
        scope = fxtr_scope.ACTIVE_SCOPE.get()
        match final:
            case FxtrID():
                final_id = final
            case EntityRef():
                final_id = final.ref
            case DataclassEntity():
                final_id = final.store().ref
            case Entity():
                final_id = scope.store_cbor(final._to_fxtr_cbor())
            case OutputStream._FinishAutomaticSentinel():
                processed_items = []
                for item in self._emitted:
                    if isinstance(item, OutputStream):
                        if not item.is_finished():
                            raise RuntimeError(
                                "Cannot finish a stream with a substream that is not finished!"
                            )
                        assert item._final_result is not None
                        processed_items.append(RawStrongFxtrID(item._final_result))
                    else:
                        processed_items.append(sendable_to_cbor(item))
                final_converted = {
                    "$fxtr.type": RawWeakFxtrID(
                        scope._main_arena.track_builtin(STREAM_OUTPUTS_TYPE_ID)
                    ),
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
                    "stream_key": sendable_to_cbor(self._stream_key),
                    "items": processed_items,
                }
                print(final_converted)
                final_id = scope.store_cbor(final_converted)
            case _:
                raise ValueError(f"Invalid final result: {final}")

        self._stream_sink.finish_with_result(final_id, scope._main_arena)
        self._final_result = final_id
        self._emitted.clear()

    @abc.abstractmethod
    def substream(self, stream_key: FxtrID | EntityRef | Entity | None = None) -> OutputStream:
        """Create a substream inside this stream.

        Args:
            stream_key: An optional key to associate this substream with.

        Returns:
            A new substream.
        """
        scope = fxtr_scope.ACTIVE_SCOPE.get()
        match stream_key:
            case FxtrID():
                stream_key_id = stream_key
            case EntityRef():
                stream_key_id = stream_key.ref
            case DataclassEntity():
                stream_key_id = stream_key.store().ref
            case Entity():
                stream_key_id = scope.store_cbor(stream_key._to_fxtr_cbor())
            case None:
                stream_key_id = None
            case _:
                raise ValueError(f"Invalid stream key: {stream_key}")
        new_sink = self._stream_sink.make_substream_sink(stream_key_id, scope._main_arena)
        new_stream = OutputStream(new_sink, stream_key_id)
        self._emitted.append(new_stream)
        return new_stream
