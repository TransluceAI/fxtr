"""The serialized surface of the entity system: ids, data values, and raw storage.

fxtr identifies values by their content: every entity has a canonical serialized form (a
:data:`FxtrDataValue`), and hashing that form yields a content ID (a CID, wrapped here as an
:class:`EntityID`). Identical content therefore always produces the same CID, which makes
deduplication automatic and lets deterministic computations reuse results safely.

The serialized form and its CID follow the DAG-CBOR / DRISL content-addressing scheme
(https://dasl.ing/drisl.html), the same one used by IPLD and the AT protocol. The concrete
serialization lives in :mod:`fxtr.core.codec`; this module defines the runtime types those bytes
map to and the *raw* storage interface that saves and loads them
(:class:`FxtrEntityDataStorage`). The typed surface — entities, references, loaders, and the
typed storage wrapper — lives in :mod:`fxtr.core.entities`.

Every stored entity is *tagged*: its serialized form is a mapping carrying a reserved
:data:`TYPE_KEY` (``"$type"``) field naming its type as a string. Storage engines enforce this on
store and load (see :func:`get_entity_type_tag`).
"""

from __future__ import annotations

import abc
import base64
import dataclasses
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from frozendict import frozendict

if TYPE_CHECKING:
    from fxtr.core.entities import BoundID, EntityLoader
    from fxtr.core.loader_registry import EntityLoaderRegistry


@dataclasses.dataclass(frozen=True)
class EntityID:
    """An identifier for an entity in the fxtr system: its content ID.

    The content ID is a DAG-CBOR / DASL CID over the entity's serialized form. It is fully
    determined by the content, so two entities with identical content have equal ids. It is a
    class of its own, rather than bare ``bytes``, so that a reference inside a data value is
    distinct from a byte string.

    Attributes:
        cid: The content ID bytes (a DAG-CBOR / DASL CID: version, codec, hash function, hash
            length, then the digest).
    """

    cid: bytes

    def cid_str(self) -> str:
        """Return the CID in DASL string form (``b`` + lowercase base32, no padding)."""
        return cid_to_str(self.cid)

    def __repr__(self) -> str:
        return f"<EntityID {self.cid_str()}>"

    def bind_type(
        self, annotation: Any, registry: EntityLoaderRegistry | None = None
    ) -> BoundID[Any]:
        """Pair this id with the loader for a target annotation, resolved through the loader
        registry, giving a :class:`~fxtr.core.entities.BoundID` that a storage can load.

        This is the way to turn an id you are holding (from an array, a config, another system, ...)
        into something loadable when you know what type it should load as. The registry lives
        above this module in the layering, so it is imported when the method is called.

        Args:
            annotation: The target annotation: a class, alias, NewType, generic alias, or union of
                these (see :meth:`~fxtr.core.loader_registry.EntityLoaderRegistry.loader_for`).
            registry: The registry to resolve the loader against; the shared default if omitted.

        Returns:
            A bound id that loads this id as ``annotation``.
        """
        from fxtr.core.loader_registry import DEFAULT_ENTITY_LOADER_REGISTRY

        if registry is None:
            registry = DEFAULT_ENTITY_LOADER_REGISTRY
        return self.bind_loader(registry.loader_for(annotation))

    def bind_loader[T](self, loader: EntityLoader[T]) -> BoundID[T]:
        """Pair this id with an explicit loader, giving a :class:`~fxtr.core.entities.BoundID`.

        Args:
            loader: The loader that deserializes the entity.

        Returns:
            A bound id that loads this id through ``loader``.
        """
        from fxtr.core.entities import BoundID

        return BoundID(entity_id=self, loader=loader)


def cid_to_str(cid: bytes) -> str:
    """Render a CID in DASL string form (``b`` + lowercase base32, no padding)."""
    return "b" + base64.b32encode(cid).decode("ascii").lower().rstrip("=")


def cid_from_str(text: str) -> bytes:
    """Parse a CID from its DASL string form (the inverse of :func:`cid_to_str`).

    Raises:
        ValueError: If ``text`` is not a ``b``-prefixed base32 string.
    """
    if not text.startswith("b") or len(text) < 2:
        raise ValueError(f"Expected a CID string starting with 'b', got {text!r}")
    body = text[1:].upper()
    try:
        return base64.b32decode(body + "=" * (-len(body) % 8))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid CID string {text!r}") from exc


type FxtrDataValue = (
    None
    | int
    | float
    | bool
    | str
    | bytes
    | EntityID
    | list[FxtrDataValue]
    | dict[str, FxtrDataValue]
)
"""A serialized fxtr data value.

This is the runtime shape of a value that maps directly onto the DAG-CBOR / DRISL dialect. Every
fxtr data object has a canonical encoding as a ``FxtrDataValue``.
"""

type FrozenFxtrDataValue = (
    None
    | int
    | float
    | bool
    | str
    | bytes
    | EntityID
    | tuple[FrozenFxtrDataValue, ...]
    | frozendict[str, FrozenFxtrDataValue]
)
"""Immutable, hashable counterpart of :data:`FxtrDataValue` (tuples and frozendicts)."""


type StorableFxtrDataValue = (
    None
    | int
    | float
    | bool
    | str
    | bytes
    | EntityID
    | Sequence[StorableFxtrDataValue]
    | Mapping[str, StorableFxtrDataValue]
)
"""The wide union of value shapes accepted when storing.

Storing is a sink: every input funnels into the same canonical wire form, so store methods accept
any mix of mutable (:data:`FxtrDataValue`) and frozen (:data:`FrozenFxtrDataValue`) shapes — both
aliases are subtypes of this one. Sequences and mappings may be any implementation (lists, tuples,
dicts, frozendicts, ...); the abstract container types make the narrower aliases assignable here
despite invariance of the concrete containers. Load methods, in contrast, always return the
canonical narrow shapes.
"""


def freeze_fxtr_data(val: StorableFxtrDataValue) -> FrozenFxtrDataValue:
    """Recursively convert any storable value shape to a :data:`FrozenFxtrDataValue`.

    Sequences become tuples and mappings become frozendicts (already-frozen parts are rebuilt
    unchanged); scalars, ``bytes``, and ``EntityID`` are already immutable and returned as-is.

    Args:
        val: The value to freeze.

    Returns:
        The frozen (hashable) equivalent of ``val``.
    """
    match val:
        case None | bool() | int() | float() | str() | bytes() | EntityID():
            return val
        case Sequence():
            return tuple(freeze_fxtr_data(v) for v in val)
        case Mapping():
            return frozendict({k: freeze_fxtr_data(v) for k, v in val.items()})
        case _:
            raise TypeError(f"Unsupported FxtrDataValue type: {type(val)}")


def thaw_fxtr_data(val: FrozenFxtrDataValue) -> FxtrDataValue:
    """Recursively convert a frozen :data:`FrozenFxtrDataValue` back to a mutable form.

    Inverse of :func:`freeze_fxtr_data`: tuples become lists and frozendicts become dicts.

    Args:
        val: The frozen value to thaw.

    Returns:
        The mutable equivalent of ``val``.
    """
    match val:
        case tuple():
            return [thaw_fxtr_data(v) for v in val]
        case frozendict():
            return {k: thaw_fxtr_data(v) for k, v in val.items()}
        case None | bool() | int() | float() | str() | bytes() | EntityID():
            return val
        case _:
            raise TypeError(f"Unsupported FrozenFxtrDataValue type: {type(val)}")


# =============================================================================
# Entity type tags
# =============================================================================

TYPE_KEY = "$type"
"""Reserved field naming a stored entity's type.

Every stored entity is a mapping carrying this key, whose value is the entity's *type id*: a
non-empty string naming the type. By convention type ids are reverse-domain names
(``org.transluce.fxtr.core.Array``); the string is the whole of what the framework knows about a
type — what a type id means (a schema, a version) is left to the program that defines it.
"""

type EntityTypeTag = str
"""The value a ``$type`` field holds (see :data:`TYPE_KEY`)."""


class InvalidEntityDataError(ValueError):
    """Raised when entity data is not a mapping carrying a valid ``$type`` tag."""


def get_entity_type_tag(data: object) -> EntityTypeTag:
    """Extract and validate the ``$type`` tag from an entity's serialized data.

    Args:
        data: The entity data value (any storable mapping shape).

    Returns:
        The tag: a non-empty string.

    Raises:
        InvalidEntityDataError: If ``data`` is not a mapping, has no :data:`TYPE_KEY` field, or
            the field's value is not a non-empty string.
    """
    if not isinstance(data, Mapping):
        raise InvalidEntityDataError(
            f"Entity data must be a mapping carrying a {TYPE_KEY!r} tag; got {type(data).__name__}"
        )
    tag = data.get(TYPE_KEY)
    if isinstance(tag, str) and tag:
        return tag
    raise InvalidEntityDataError(
        f"Entity data must carry a {TYPE_KEY!r} tag that is a non-empty string; got {tag!r}"
    )


# =============================================================================
# Raw storage interface
# =============================================================================


class EntityNotFoundError(KeyError):
    """Raised when a storage engine is asked to load an entity it does not have."""


class UnknownReferenceError(ValueError):
    """Raised when storing a value that references an entity the store does not yet know.

    Storage must be able to load every referenced id before a referencing value can be stored;
    this preserves the invariant that any loadable id has a fully loadable subgraph.
    """


class FxtrEntityDataStorage(abc.ABC):
    """Interface for saving and loading raw entity data.

    A storage engine maps between serialized entity data and EntityIDs. It is responsible for
    maintaining the closed subgraph invariant: If a value references the id of another entity,
    that entity must already be known to the store before the referencing value can be stored,
    and it stays loadable for as long as the referencing value is loadable.

    Storing and loading are both asynchronous in this interface, so that an engine may sit behind
    a process or network boundary and delegate the work — computing ids included — to the other
    side. The asynchrony is a property of the interface, not a demand on engines: an engine that
    can complete a store without waiting (an in-process dict, a write-behind buffer flushed later
    in a transaction of its own) simply does so and returns, and such an engine may also expose
    synchronous twins of these methods for in-process callers that cannot await.

    To store without a round trip, an engine may reject values that reference ids it does not
    currently know are present; how "known" is determined is up to the engine (see
    :meth:`is_known`).

    Every stored entity is tagged: engines validate that stored data is a mapping carrying a valid
    :data:`TYPE_KEY` tag (see :func:`get_entity_type_tag`), and reject untagged data on load.

    This class speaks only the raw serialized types; the typed store/load surface (entities,
    references, loaders) is :class:`fxtr.core.entities.FxtrEntityStorage`.
    """

    @abc.abstractmethod
    def is_known(self, entity_id: EntityID) -> bool:
        """Return whether ``entity_id`` is currently known to be present in this storage.

        An id is known as soon as it is returned by :meth:`store_raw_data` or appears in the
        result of :meth:`load_raw_data`. An engine may also treat other ids as known (e.g. IDs
        that have been retrieved by named aliases).

        Args:
            entity_id: The id to check.

        Returns:
            ``True`` if the entity can be loaded from this storage, or referenced by data stored
            into this storage.
        """

    @abc.abstractmethod
    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        """Store a serialized value and return its content id.

        Args:
            data: The value to save: a mapping carrying a valid :data:`TYPE_KEY` tag. Every
                ``EntityID`` referenced by ``data`` must already be known to the store.

        Returns:
            The canonical CID of the value, as an id. Loading it returns the data that was
            stored.

        Raises:
            InvalidEntityDataError: If ``data`` is not a mapping carrying a valid
                :data:`TYPE_KEY` tag.
            UnknownReferenceError: If ``data`` references an id that is not known to the store.
        """

    @abc.abstractmethod
    async def load_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        """Load a serialized value by id.

        Args:
            entity_id: The id to load. Must be known to the storage engine.

        Returns:
            The data for this entity, whose content matches the content ID of ``entity_id``.

        Raises:
            EntityNotFoundError: If ``entity_id`` is not known to be present in the storage engine.
            InvalidEntityDataError: If the stored data is not a validly tagged entity mapping.
        """

    async def load_raw_frozen_data(self, entity_id: EntityID) -> FrozenFxtrDataValue:
        """Load a value by id and return it in frozen form. See :meth:`load_raw_data`."""
        return freeze_fxtr_data(await self.load_raw_data(entity_id))
