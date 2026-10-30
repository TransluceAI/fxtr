"""Alternative (not currently used) representation of Fxtr collections."""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import sys
import types
import warnings
import weakref
from typing import Generator, Mapping, Protocol, Sequence, TypeVar

from frozendict import frozendict

from . import core, dag_util

SpecialTrackable = core.UserDefinedEntityMeta | Protocol

T_Trackable = TypeVar("T_Trackable", bound=core.AnyEntity | SpecialTrackable)

NestedTrackable = (
    str
    | int
    | float
    | bool
    | bytes
    | None
    | Mapping[str, "NestedTrackable"]
    | Sequence["NestedTrackable"]
    | core.AnyEntity
    | SpecialTrackable
)

"""A mapping from Fxtr entities to the collection builders that created them, if any."""
_FXTR_ENTITY_TO_BUILDER: weakref.WeakKeyDictionary[core.AnyEntity, FxtrCollectionBuilder | None] = (
    weakref.WeakKeyDictionary()
)


class FxtrCollectionConversionError(Exception):
    """Error raised when converting a Python object to a Fxtr collection."""


####################################################################################################
# region: Collection object
####################################################################################################


class Collection(core.DataEntityBase, type_id="Collection^1", namespace_id="!builtin"):
    """A redistributable collection in the fxtr system.

    Fxtr collections are named collections of collection data, with a namespace that identifies their owner
    and (usually) a human-readable name allowing them to be referred to easily. collections are the
    main entrypoints into the fxtr system from external tools, and are also the unit uploaded to
    the hub.

    Note that it is possible to have multiple collections defined that have the same namespace and name.
    This usually represents different _versions_ of the same collection. Usually user code will only
    interact with one version of a collection at a time.

    The namespace ID allows us to enforce ownership restrictions:
    - Only the owner of a namespace can upload collections that either (a) use that namespace, or (b)
      define new types that use that namespace.
    - Users with read access to a namespace can upload collections that _reference_ collections in that
      namespace.

    Attributes:
        namespace_id: The namespace of the collection.
        name: The name of the collection.
        items: The named items in the collection.
        anonymous_exports: Other entities exported by the collection without a name. Usually these will
            be instead referenced by a CID link elsewhere in the collection.
        references: CIDs of other collections that this collection depends on, was built from, or is
            related to. Used for provenance tracking, name assignment, and permissions. May also be
            used for storage purposes: entities already present in one of these references does not
            need to need to be copied again.
    """

    namespace_id: str
    name: str | None
    items: frozendict[str, core.AnyFrozenIPLDable]
    anonymous_exports: frozenset[core.AnyEntity]
    references: frozenset[core.AnyEntity]  # TODO: fix this


####################################################################################################
# region: Special trackable conversion
####################################################################################################


class CollectionDefn_Class(
    core.DataEntityBase, type_id="CollectionDefn_Class^1", namespace_id="!builtin"
):
    """Placeholder for a data entity class in a collection.

    When a Python data entity class is defined in a Python module, its runtime representation is an
    actual Python class, so that it can be instantiated. This also means it is _not_ directly a
    fxtr entity. However, we may still want to refer to that class from a Fxtr collection with a
    name.

    This entity stands in for that Python class in a collection, and should be treated as equivalent
    to it when viewed from Python.
    """

    value_typespec: core.TypeSpecBase


class CollectionDefn_Escaped(
    core.DataEntityBase, type_id="CollectionDefn_Escaped^1", namespace_id="!builtin"
):
    """Placeholder for an escaped value in a collection.

    When a Python value is escaped in a collection, it is converted to an entity that stands in for
    it. However, you might for some reason want to store that actual stand-in entity inside a
    collection. This entity stands in for that stand-in entity.
    """

    escaped: core.AnyEntity


CollectionDefnTypes = CollectionDefn_Class | CollectionDefn_Escaped


def _wrap_for_collection(trackable: core.AnyEntity | SpecialTrackable) -> core.AnyEntity:
    """Wrap an entity or special trackable into a collection-compatible form.

    Args:
        trackable: The entity or special trackable to convert.

    Returns:
        An entity representing the same value as the input, but which can be stored inside a
        collection. If `trackable` was a normal entity, it will be returned unchanged. If it was a
        special trackable Python type or a special collection entity, it will be wrapped for safe
        storage inside a collection.
    """
    # Implementation note:
    match trackable:
        case core.UserDefinedEntityMeta(value_typespec=core.TypeSpec_DataEntity() as vt):
            return CollectionDefn_Class(value_typespec=vt)
        case Protocol():
            raise NotImplementedError("Protocols are not yet supported")
        case CollectionDefn_Class() | CollectionDefn_Escaped():
            # Escape it!
            return CollectionDefn_Escaped(escaped=trackable)
        case core.AnyEntity():
            return trackable
        case _:
            raise ValueError(f"Cannot convert {type(trackable).__name__} to an entity")


def _unwrap_from_collection(entity: core.AnyEntity) -> core.AnyEntity | SpecialTrackable:
    """Convert an entity to a special trackable.

    Args:
        entity: The entity to unwrap.

    Returns:
        The entity or special trackable that the input entity represents. If the input entity was a
        normal entity, it will be returned unchanged. If it was a special collection entity, it will
        be unwrapped into the special trackable that it represents.
    """
    match entity:
        case CollectionDefn_Class(value_typespec=value_typespec):
            if isinstance(value_typespec, core.TypeSpec_DataEntity):
                return value_typespec._get_py_class()
            else:
                raise ValueError(f"Unrecognized value typespec: {type(value_typespec).__name__}")
        case CollectionDefn_Escaped(escaped=escaped):
            return escaped
        case core.AnyEntity():
            return entity
        case _:
            raise ValueError(f"Cannot convert {type(entity).__name__} to a special trackable")


def _wrap_nested_for_collection(trackable: NestedTrackable) -> core.AnyFrozenIPLDable:
    """Wrap a nested trackable into a collection-compatible form.

    Args:
        trackable: The nested trackable to convert.

    Returns:
        A collection-compatible form of the input.
    """
    if isinstance(trackable, str | int | float | bool | bytes | None):
        return trackable
    elif isinstance(trackable, Mapping):
        return frozendict(
            {name: _wrap_nested_for_collection(item) for name, item in trackable.items()}
        )
    elif isinstance(trackable, Sequence):
        return tuple(_wrap_nested_for_collection(item) for item in trackable)
    elif isinstance(trackable, core.AnyEntity | SpecialTrackable):
        return _wrap_for_collection(trackable)
    else:
        raise FxtrCollectionConversionError(
            f"Cannot add {type(trackable).__name__} to a collection: not a recognized type"
        )


# endregion
####################################################################################################
# region: Manual collection builder
####################################################################################################


"""The active collection builder, if any."""
_ACTIVE_BUILDER: contextvars.ContextVar[FxtrCollectionBuilder | None] = contextvars.ContextVar(
    "_ACTIVE_BUILDER", default=None
)


"""Collections associated with Python modules."""
_PYTHON_MODULE_COLLECTIONS: weakref.WeakKeyDictionary[types.ModuleType, Collection] = (
    weakref.WeakKeyDictionary()
)


class FxtrCollectionBuilder:
    """A builder for Fxtr collections.

    This builder can be used to simplify the construction of Fxtr collections, by automatically
    tracking module dependencies through Python imports.
    """

    def __init__(self, namespace_id: str, name: str | None):
        self._namespace_id = namespace_id
        self._name = name
        self._anonymous_exports: set[core.AnyEntity] = set()
        self._open: bool = False
        self._collection: Collection | None = None
        self._known_entities: frozenset[core.AnyEntity] | None = None

    def track(self, entity: T_Trackable) -> T_Trackable:
        """Track an anonymous export of an entity.

        Args:
            entity: The entity to track.
        """
        if not self._open:
            raise ValueError("Collection not open! Cannot add exports.")
        if self._collection is not None:
            raise ValueError("Collection already built! Cannot add more exports.")

        wrapped = _wrap_for_collection(entity)
        self._anonymous_exports.add(wrapped)

        return entity  # type: ignore

    def _track_pre_wrapped(self, wrapped_entity: core.AnyEntity) -> None:
        self._anonymous_exports.add(wrapped_entity)

    def build(self, items: Mapping[str, NestedTrackable]) -> Collection:
        """Build a collection object.

        Args:
            items: The items to include in the collection. Each value can be an entity, a subclass
                of DataEntityBase, a fxtr protocol definition, or a (frozen)dict, list, or tuple
                of such values.

        Returns:
            The collection object.
        """
        if not self._open:
            raise ValueError(
                "Collection not open! Please build the collection before exiting the context"
                " manager."
            )
        assert self._collection is None, "Collection already built!"

        # Flatten and convert the items.
        # Note: Wrapper objects are always created inside this collection builder, so they always
        # associate themselves with this builder. This is what we want because we don't want them
        # to unnecessarily introduce any extra dependencies.
        # TODO(daniel): maybe there is a more obvious way to do this.
        wrapped_items = frozendict(
            {name: _wrap_nested_for_collection(item) for name, item in items.items()}
        )
        return self._build_pre_wrapped(wrapped_items)

    def _build_pre_wrapped(
        self, wrapped_items: frozendict[str, core.AnyFrozenIPLDable]
    ) -> Collection:

        # Find entities reachable from this collection. Stop as soon as we see an entity that was
        # built outside of this collection builder.
        dep_collector = dag_util.RecursiveSubgraphCollector(
            include_if=lambda ent: (_FXTR_ENTITY_TO_BUILDER.get(ent) is self)
        )
        item_spec = core.TypeSpec_AnyFrozenIPLDable()
        for value in wrapped_items.values():
            dep_collector.visit_part(spec=item_spec, value=value)
        for entity in dep_collector.visited_entities:
            dep_collector.visit_entity(entity)

        # Infer references from the reachable (excluded) entities.
        references: set[Collection] = set()
        for foreign_entity in dep_collector.excluded_entities:
            foreign_builder = _FXTR_ENTITY_TO_BUILDER.get(foreign_entity)
            if foreign_builder is None:
                if isinstance(
                    foreign_entity, core.DataEntitySchema | core.ExternalServiceIdentifier
                ):
                    if foreign_entity.namespace_id.startswith("!builtin"):
                        # Builtin entities are not part of any explicit collection and don't have
                        # to be referenced; they are always available if known to the library,
                        # and are rejected if defined by the user.
                        continue
                shortrepr = (
                    repr(foreign_entity)[:100] + "..."
                    if len(repr(foreign_entity)) > 100
                    else repr(foreign_entity)
                )
                warnings.warn(
                    "Found an entity with unknown provenance (type:"
                    f" {type(foreign_entity).__name__}, cid: {core.entity_cid(foreign_entity)})"
                    " that was built outside of this collection builder. Treating it as part of"
                    f" collection {self._namespace_id}/{self._name}. This may cause duplication."
                    f" Value: {shortrepr}"
                )
            elif foreign_builder._collection is None:
                raise ValueError(
                    f"While building collection {self._namespace_id}/{self._name}, found an entity"
                    " from another incomplete collection"
                    f" ({foreign_builder._namespace_id}/{foreign_builder._name}). This is not"
                    " supported! Make sure to finalize the other collection using `build(...)`"
                    " before depending on it in this collection."
                )
            else:
                assert foreign_builder._known_entities is not None
                if foreign_entity not in foreign_builder._known_entities:
                    raise ValueError(
                        f"While building collection {self._namespace_id}/{self._name}, found an"
                        " entity that was defined inside another collection"
                        f" ({foreign_builder._namespace_id}/{foreign_builder._name}) but was not"
                        " exported from that collection! This is not supported. When building a"
                        " collection, all entities defined in the collection's scope should be"
                        " explicitly exported by that collection (using either `build` or"
                        " `track`)."
                    )
                references.add(foreign_builder._collection)

        # Find entities exported from this collection, so that later collection builders can
        # identify them.
        known_entities = set()
        for entity in dep_collector.visited_entities:
            assert _FXTR_ENTITY_TO_BUILDER.get(entity) is self
            known_entities.add(entity)

        self._known_entities = frozenset(known_entities)
        self._collection = Collection(
            namespace_id=self._namespace_id,
            name=self._name,
            items=wrapped_items,
            anonymous_exports=frozenset(self._anonymous_exports),
            references=frozenset(references),
        )
        return self._collection


@contextlib.contextmanager
def build_collection(
    namespace_id: str, name: str | None
) -> Generator[FxtrCollectionBuilder, None, None]:
    """Context manager for building a collection.

    This context manager can be used to build a collection in a scope. All entities defined in the
    scope will be tracked as belonging to this collection, so that any other builder that includes
    those entities will automatically be tracked as depending on this collection. Similarly,
    any entities included in this collection that were defined inside another collection's scope
    will be tracked as references of this collection.

    Additionally, all data entity types and external service identifiers defined in this scope will
    have their namespace ID set to the collection's namespace ID if not explicitly set.

    Intended for use like:

    ```python
    with build_collection(namespace_id, name) as builder:
        items = {}
        # Define named entities in the collection
        items["some_name"] = SomeEntity(...)
        # Possibly track anonymous exports
        builder.track(SomeOtherEntity(...))

        # Finish building the collection
        collection = builder.build(items)
    ```

    Args:
        namespace_id: The namespace of the collection.
        name: The name of the collection.

    Returns:
        A builder for the collection. You should call `build` on the builder to finish building the
        collection.
    """
    builder = FxtrCollectionBuilder(namespace_id, name)

    prev_creation_hook = core._ENTITY_CREATION_HOOK.get()

    def _collection_creation_hook(entity: core.AnyEntity):
        _FXTR_ENTITY_TO_BUILDER[entity] = builder
        if prev_creation_hook is not None:
            prev_creation_hook(entity)

    with core._using_type_namespace_id(namespace_id):
        active_builder_token = _ACTIVE_BUILDER.set(builder)
        creation_hook_token = core._ENTITY_CREATION_HOOK.set(_collection_creation_hook)
        try:
            builder._open = True
            yield builder
            builder._open = False
            if builder._collection is None:
                raise ValueError(
                    "Collection not built! Please call `build` on the builder before exiting the"
                    " context manager."
                )
        finally:
            _ACTIVE_BUILDER.reset(active_builder_token)
            core._ENTITY_CREATION_HOOK.reset(creation_hook_token)


# endregion
####################################################################################################
# region: Collection modules
####################################################################################################


@dataclasses.dataclass(frozen=True)
class _UseModuleName:
    """Placeholder for the `export_as` argument to `define_collection_module`."""


@contextlib.contextmanager
def define_collection_module(
    module_name: str,
    *,
    namespace_id: str,
    export_as: str | None | _UseModuleName = _UseModuleName(),
) -> Generator[FxtrCollectionBuilder, None, None]:
    """Context manager for defining a collection module.

    This context manager can be used to mark the current Python module as defining a Fxtr
    collection, whose items are the entities defined in the module.

    Unlike `build_collection`, this context manager does not require you to call `build` on the
    builder. Instead, the collection will be built from the globals in the Python module when the
    context manager exits.

    Args:
        module_name: The name of the current Python module. Should always be set to __name__ by the
            caller.
        namespace_id: The namespace of the collection.
        export_as: The name of the collection. If not provided, the collection will be named equal
            to the module name.
    """
    if module_name not in sys.modules:
        raise ValueError(f"Module {module_name} is not a module in sys.modules!")
    match export_as:
        case _UseModuleName():
            if module_name == "__main__":
                raise ValueError(
                    "Cannot infer a collection name from the module name: the module's name is"
                    " __main__! Make sure collection modules are imported from main scripts rather"
                    " than being executed directly."
                )
            collection_name = module_name
        case _:
            collection_name = export_as
    with build_collection(namespace_id, collection_name) as builder:
        yield builder
        if builder._collection is not None:
            raise ValueError(
                "Collections should not be built manually when using `define_collection_module`; it"
                " will be built automatically at contextmanager exit."
            )

        # Walk the items.
        mod = sys.modules[module_name]
        items = _walk_collection_module(mod)
        collection = builder._build_pre_wrapped(items)
        _PYTHON_MODULE_COLLECTIONS[mod] = collection


def _walk_collection_module(module: types.ModuleType) -> frozendict[str, core.AnyFrozenIPLDable]:
    """Extract the items from a collection module."""
    if "__all__" in module.__dict__:
        all_names = module.__dict__["__all__"]
    else:
        all_names = [
            key
            for key, value in module.__dict__.items()
            if not key.startswith("_")
            and not isinstance(value, types.ModuleType | types.FunctionType)
        ]

    result = {}
    for name in all_names:
        value = module.__dict__[name]
        try:
            result[name] = _wrap_nested_for_collection(value)
        except FxtrCollectionConversionError as exc:
            exc.add_note(
                f"This error occured while converting identifier {repr(name)} in module"
                f" {module.__name__}. If this identifier is private and should not be exported,"
                " consider defining __all__ in your module and excluding it."
            )
            raise
    return frozendict(result)


def extract_collection_from_module(module: types.ModuleType) -> Collection:
    """Get the collection module for a given module (by name)."""
    if module not in _PYTHON_MODULE_COLLECTIONS:
        raise ValueError(f"Module {module.__name__} is not a fxtr collection module!")
    return _PYTHON_MODULE_COLLECTIONS[module]


# endregion
####################################################################################################
