"""The typed surface of the entity system: entities, references, and loaders.

This module builds the typed store/load experience on top of the raw serialized surface in
:mod:`fxtr.core.entity_data`:

- An :class:`Entity` knows how to serialize itself and which :class:`EntityLoader` deserializes
  it; :class:`FxtrEntityStorage` stores one, yielding a :class:`BoundID` bundling the
  :class:`EntityID` with that loader.
- An :class:`EntityLoader` converts stored data into Python values and can be *asked* whether it
  supports a given entity type, which makes loaders composable: a :class:`UnionEntityLoader`
  dispatches a conversion among member loaders by the entity's ``$type`` tag.
- :class:`FxtrEntityStorage` extends the raw :class:`FxtrEntityDataStorage` contract with the
  typed ``store`` / ``load`` wrappers.

Two symmetric purity rules keep the layering simple. Serialization never stores: an entity turns
itself into data and names its loader, and only the storage wrapper stores anything. Conversion
never loads: a loader turns data it is handed into a value, and only the storage wrapper fetches
anything. So the storage engine is the one place fetching and writing happen, and a loader or an
entity works the same against any engine — or with no engine at all.

Layering note: this module never inspects type *annotations* — a bound id carries a loader
object, not an annotation. Turning an annotation into a loader is the loader registry's job (a
layer above), and no annotation-taking convenience should be added here. The one entry point that
crosses the layers is :meth:`fxtr.core.entity_data.EntityID.bind_type`, which imports the registry
at call time.

Asymmetry note: Loading a BoundID to an Entity and then storing it should always produce an equal
content ID. Storing an Entity to a BoundID and then loading it back to an Entity should produce
an "equivalent" entity, but the exact equality relationship is up to the Entity class (see Entity
docstring).
"""

from __future__ import annotations

import abc
import dataclasses
from collections.abc import Sequence
from typing import Any, Generic, Self, TypeVar, overload

from fxtr.core import codec
from fxtr.core.entity_data import (
    EntityID,
    FxtrEntityDataStorage,
    StorableFxtrDataValue,
    get_entity_type_tag,
)


class EntityLoader[T](abc.ABC):
    """Deserializes entities from their stored form into Python values.

    A loader is the *conversion* half of a reference: :class:`BoundID` pairs an id with the
    loader that can turn its data into a ``T``. Conversion is pure — a loader is handed the
    entity's data and never loads anything itself — so a loader works against any storage engine.
    Loaders also answer whether they support a given entity type, which lets the framework compose
    them into tagged-union dispatchers (:class:`UnionEntityLoader`).
    """

    @abc.abstractmethod
    def supports_type(self, type_id: str) -> bool:
        """Return whether this loader can convert entities tagged with ``type_id``.

        Must be deterministic per ``(loader, type_id)`` and side-effect-free.

        Args:
            type_id: The entity type (the ``$type`` field of the entity's data).

        Returns:
            ``True`` if :meth:`convert` can convert entities of this type.
        """

    @property
    def static_type_id(self) -> str | None:
        """The one entity type this loader converts, when that is known up front.

        A loader for exactly one type may report it here, so that code holding only the loader (a
        :class:`BoundID`, say) can name the type of what it points to without loading it. Loaders
        whose support spans several types (unions, the wildcard) return ``None``, the default.
        When not ``None``, ``supports_type`` must hold for exactly this type.
        """
        return None

    @abc.abstractmethod
    def convert(self, data: StorableFxtrDataValue) -> T:
        """Convert an entity's serialized data into a ``T``.

        Pure: the data is complete as given (references inside it are ids, converted lazily into
        bound ids or kept as ids, never followed), so nothing is loaded here.

        Args:
            data: The entity's data, in any storable shape: the mutable form a storage engine
                returns, or a frozen form.

        Returns:
            The converted value. Re-storing it should round-trip to the same content id.
        """


class StaticTypeLoader[T](EntityLoader[T]):
    """Loader base implementing the support check from a statically-declared type.

    The common case: a loader that converts exactly one entity type, known up front. Subclasses
    set :attr:`_static_type_id` (e.g. in their constructor) and implement :meth:`convert`; loaders
    whose support spans several types implement ``supports_type`` directly instead.
    """

    _static_type_id: str

    def supports_type(self, type_id: str) -> bool:
        return type_id == self._static_type_id

    @property
    def static_type_id(self) -> str | None:
        return self._static_type_id


class NoMatchingVariantError(ValueError):
    """Raised when tagged-union dispatch finds no member supporting the entity's type."""


class AmbiguousVariantError(ValueError):
    """Raised when tagged-union dispatch finds multiple members supporting the entity's type."""


class UnionEntityLoader[T](EntityLoader[T]):
    """Dispatches a conversion among member loaders by the entity's ``$type`` tag.

    Members are consulted via ``supports_type``; exactly one must claim the tag. Disjointness is
    enforced at dispatch time, with errors that list the candidates.
    """

    def __init__(self, members: Sequence[tuple[Any, EntityLoader[Any]]]) -> None:
        """Initialize the union loader.

        Args:
            members: ``(label, loader)`` pairs; the label (typically the member's type
                annotation) is used in error messages only.
        """
        self._members = tuple(members)

    def supports_type(self, type_id: str) -> bool:
        return any(ldr.supports_type(type_id) for _, ldr in self._members)

    def convert(self, data: StorableFxtrDataValue) -> T:
        tag = get_entity_type_tag(data)
        matches = [(ann, ldr) for ann, ldr in self._members if ldr.supports_type(tag)]
        if not matches:
            tried = ", ".join(repr(ann) for ann, _ in self._members)
            raise NoMatchingVariantError(
                f"No union member supports entity type {tag!r}. Tried: {tried}"
            )
        if len(matches) > 1:
            matched = ", ".join(repr(ann) for ann, _ in matches)
            raise AmbiguousVariantError(
                f"Multiple union members support entity type {tag!r}: {matched}"
            )
        return matches[0][1].convert(data)


# The reference types are *covariant* in their target type: a ref is a read-only handle whose
# loader produces a T (a BoundID[Sub] is usable wherever a BoundID[Base] is expected).
# The variance is declared with an old-style TypeVar because PEP 695 only infers variance, and
# on Python 3.13+ the dataclass-synthesized ``__replace__`` puts T in a parameter position,
# which would make the inferred variance invariant.
T_co = TypeVar("T_co", covariant=True)


@dataclasses.dataclass(frozen=True)
class BoundID(Generic[T_co]):
    """A reference to a stored entity, bundled with the loader that deserializes it.

    An ``BoundID`` carries a converter that knows how to convert the raw data bytes for the entity
    into a runtime Python type.

    To build one from a bare id, use :meth:`~fxtr.core.entity_data.EntityID.bind_type` (a
    *target annotation*, resolved through the loader registry) or
    :meth:`~fxtr.core.entity_data.EntityID.bind_loader` (an explicit loader).

    Attributes:
        entity_id: The id of the referenced entity.
        loader: A loader that deserializes the entity into its Python form. Loading followed by
            re-storing should round-trip to the same content id.
    """

    entity_id: EntityID
    loader: EntityLoader[T_co]


_UNSET: Any = object()
"""Sentinel for an omitted ``as_type`` (``None`` is a legitimate, if odd, annotation)."""


class FxtrEntityStorage(FxtrEntityDataStorage, abc.ABC):
    """Entity storage with the typed store/load surface.

    Extends the raw :class:`FxtrEntityDataStorage` contract with wrappers that speak
    :class:`Entity` and :class:`BoundID`: storing serializes the entity and stores the data,
    pairing the id with the entity's own loader; loading fetches the data and hands it to a
    loader, either the one a bound id carries or one named at the call (``as_type`` /
    ``with_loader``). Storage engines subclass this and implement the raw methods; these wrappers
    are the same for every engine.
    """

    async def store[T: Entity](self, entity: T) -> BoundID[T]:
        """Store an entity, returning a bound id (wrapper over :meth:`store_raw_data`)."""
        entity_id = await self.store_raw_data(entity._fxtr_serialize())
        return BoundID(entity_id=entity_id, loader=entity._fxtr_loader())

    @overload
    async def load[T](self, entity_id: BoundID[T]) -> T: ...
    @overload
    async def load[T](self, entity_id: EntityID | BoundID[Any], *, as_type: type[T]) -> T: ...
    @overload
    async def load(self, entity_id: EntityID | BoundID[Any], *, as_type: Any) -> Any: ...
    @overload
    async def load[T](
        self, entity_id: EntityID | BoundID[Any], *, with_loader: EntityLoader[T]
    ) -> T: ...

    async def load(
        self,
        entity_id: EntityID | BoundID[Any],
        *,
        as_type: Any = _UNSET,
        with_loader: EntityLoader[Any] | None = None,
    ) -> Any:
        """Load an entity: fetch its data (:meth:`load_raw_data`) and convert it.

        The loader comes from one of three places, in this order of precedence: ``with_loader``,
        an explicit loader; ``as_type``, a target annotation resolved through the loader registry
        (as :meth:`~fxtr.core.entity_data.EntityID.bind_type` does); or the loader a
        :class:`BoundID` carries. At most one of ``as_type`` and ``with_loader`` may be given, and
        a bare :class:`~fxtr.core.entity_data.EntityID` needs one of them.

        Args:
            entity_id: The id to load, bound or bare.
            as_type: The annotation to load the entity as; overrides a bound id's loader.
            with_loader: The loader to load the entity with; overrides a bound id's loader.

        Raises:
            TypeError: If both ``as_type`` and ``with_loader`` are given, or neither is and
                ``entity_id`` is not bound.
        """
        if with_loader is not None and as_type is not _UNSET:
            raise TypeError("load() takes at most one of as_type= and with_loader=")
        bare = entity_id.entity_id if isinstance(entity_id, BoundID) else entity_id
        if with_loader is not None:
            loader = with_loader
        elif as_type is not _UNSET:
            loader = bare.bind_type(as_type).loader
        elif isinstance(entity_id, BoundID):
            loader = entity_id.loader
        else:
            raise TypeError(
                "load() of a bare EntityID needs as_type= or with_loader= (or bind the id first"
                " with EntityID.bind_type / bind_loader)"
            )
        return loader.convert(await self.load_raw_data(bare))


class Entity(abc.ABC):
    """Base class for the runtime representation of a fxtr entity.

    An entity knows how to serialize itself into a :data:`fxtr.core.entity_data.FxtrDataValue`
    and which loader deserializes it; :meth:`FxtrEntityStorage.store` does the storing. Concrete
    subclasses define their own serialization.
    """

    @classmethod
    def fxtr_type_id(cls) -> str | None:
        """The type every instance of this class is tagged with, if the class knows it
        statically.

        ``None`` means the type is not known from the class alone: the class is abstract, or its
        instances carry their own type (:class:`~fxtr.entity_defns.unknown_entity.UnknownEntity`).
        Concrete tagged classes override this; the default is ``None``.
        """
        return None

    def content_id(self) -> EntityID:
        """The id this entity has as a stored entity, computed locally.

        Every store returns this id for this entity; nothing is stored here. Content-addressed
        keys that are never stored — a runner node's identity, a step request's fingerprint —
        are this.
        """
        data = self._fxtr_serialize()
        return EntityID(cid=codec.compute_cid(codec.encode(data)))

    @abc.abstractmethod
    def _fxtr_serialize(self) -> StorableFxtrDataValue:
        """Serialize this entity to its data value form: a mapping carrying a valid ``$type`` tag.

        Pure: nothing is stored here (see the module docstring); references to other entities
        appear as their ids. Structurally, the serialization of an entity must be fully determined
        by its runtime representation, not its type annotations or generic type variables.

        Loading the stored data back through :meth:`_fxtr_loader` does not need to strictly
        produce an object equal to ``self``, but the values should be "equivalent" according to
        the program. Some information may be lost, e.g. canonicalizing lists to tuples, or losing
        specific runtime types that are not roundtrippable through the serialized form.
        """

    @abc.abstractmethod
    def _fxtr_loader(self) -> EntityLoader[Self]:
        """The loader that deserializes what :meth:`_fxtr_serialize` produced.

        Called by :meth:`FxtrEntityStorage.store` to build the returned :class:`BoundID`.
        """
