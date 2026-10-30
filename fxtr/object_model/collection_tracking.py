"""Building and ownership tracking for Fxtr collection data.

Many Fxtr collections are created from Python modules. To make this work well, it is useful to capture
dependencies between Fxtr collections by piggy-backing on the Python module system, instead of forcing
everyone to list all their dependencies manually.

This library enables this by storing a mapping between Python entity objects and the Fxtr collections
that they belong to. It also handles conversion of certain special Python types into "tracked" forms
so that they can be saved inside Fxtr collections.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import sys
import types
import warnings
import weakref
from typing import Annotated, Any, Generator, Iterable, Mapping, Sequence, TypeVar

from frozendict import frozendict

from fxtr.object_model import modulescoping

from . import core, dag_util, entity_util

logger = logging.getLogger(__name__)

SpecialTrackable = core.UserDefinedEntityMeta | type[Any]

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


class FxtrCollectionConversionError(Exception):
    """Error raised when converting a Python object to a Fxtr collection."""


####################################################################################################
# region: Collection types
####################################################################################################

# Note: Entity key types are hardcoded into the system and are awkward to encode as entities, so we
# instead manipulate them as dataclasses and save them when needed as JSON blobs. This should be fine
# because we will always know that these are supposed to be entity keys from context due to where
# they are used.


@dataclasses.dataclass(frozen=True)
class LocalEntityKey:
    """Key for an entity defined in the current blueprint."""

    definition_offset: int

    def to_ipld(self) -> core.AnyIPLDable:
        return {
            "tag": "local",
            "definition_offset": self.definition_offset,
        }


@dataclasses.dataclass(frozen=True)
class ImportedEntityKey:
    """Key for an entity defined in a different blueprint."""

    module: Collection
    definition_offset: int

    def to_ipld(self) -> core.AnyIPLDable:
        return {
            "tag": "import",
            "module": self.module,
            "definition_offset": self.definition_offset,
        }


@dataclasses.dataclass(frozen=True)
class UnknownEntityKey:
    """Key for an entity that is not known to the system."""

    cid_b32: str

    def to_ipld(self) -> core.AnyIPLDable:
        return {"tag": "unknown", "cid": self.cid_b32}


@dataclasses.dataclass(frozen=True)
class EntityAtPathInfo:
    """Information about a child entity at a specific path."""

    py_path: core.AccessPath
    ipld_path: core.AccessPath
    entity_key: LocalEntityKey | ImportedEntityKey | UnknownEntityKey

    def to_ipld(self) -> core.AnyIPLDable:
        return {
            "py_path": core.freeze(self.py_path, freeze_hook=core.call_to_ipld),
            "ipld_path": core.freeze(self.ipld_path, freeze_hook=core.call_to_ipld),
            "entity_key": self.entity_key.to_ipld(),
        }


@dataclasses.dataclass(frozen=True)
class ReexportedEntityInfo:
    """Information about a re-exported entity in a module."""

    py_path: core.AccessPath
    entity_key: ImportedEntityKey | UnknownEntityKey

    def to_ipld(self) -> core.AnyIPLDable:
        return {
            "py_path": core.freeze(self.py_path, freeze_hook=core.call_to_ipld),
            "entity_key": self.entity_key.to_ipld(),
        }


@dataclasses.dataclass
class ConvertedFrom:
    """Information about the original form of a value, for use in Annotated."""

    python_type: Any


FrozenIPLD = core.AnyFrozenIPLDable


class LocalEntityDefn(
    core.DataEntityBase, type_id="LocalEntityDefn^1", namespace_id="!builtin:module_meta"
):
    """Metadata for a definition for an entity, as defined in a specific blueprint.

    An entity definition combines a specific entity (uniquely identified by its contents) with
    metadata about how it was defined / how it is accessed in a particular blueprint, and also
    where its children (any entities that it contains) were defined. This metadata can be tracked
    while the module is being executed (for debugging and refactoring purposes), but does not
    affect the definition of the entity for the purposes of e.g. experiments that use it.

    Attributes:
        definition_offset: The index in the sequence of created entities in this module where this
            entity was created.
        entity: The entity that was defined.
        canonical_access_path: The canonical access path for this entity, if any. Inferred via
            heuristics during module execution and postprocessing.
        aliases: Aliases for this entity, by which it is accessible in this module. Inferred via
            heuristics during module execution and postprocessing. May not include every access
            path.
        child_entities: Definitions for the other entities that this entity contains. Inferred via
            Python object identity (which is metadata not tracked by the entity graph itself).
            Useful for debugging and navigating the entity graph.
        documentation: Documentation for this entity, if any. Extracted on a case-by-case basis
            from metadata collected during module execution.
    """

    definition_offset: int
    entity: core.EntityBase
    canonical_access_path: Annotated[FrozenIPLD, ConvertedFrom(core.AccessPath | None)]
    aliases: tuple[Annotated[FrozenIPLD, ConvertedFrom(core.AccessPath)], ...]
    child_entities: tuple[Annotated[FrozenIPLD, ConvertedFrom(EntityAtPathInfo)], ...]
    documentation: str | None


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
        dependencies: CIDs of other collections that this collection depends on, was built from, or is
            related to. Used for provenance tracking, name assignment, and permissions. May also be
            used for storage purposes: entities already present in one of these dependencies does not
            need to need to be copied again.
        entity_defns: Definitions for the entities created as part of the collection.
        re_exported_entities: Other entities exported by the collection but defined elsewhere.
    """

    namespace_id: str
    name: str | None
    dependencies: frozenset[Collection]
    entity_defns: tuple[LocalEntityDefn, ...]
    re_exported_entities: tuple[Annotated[FrozenIPLD, ConvertedFrom(ReexportedEntityInfo)], ...]


# endregion
####################################################################################################
# region: Manual collection builder
####################################################################################################


@dataclasses.dataclass
class _AliasState:
    """Mutable state for tracking entities and canonical paths during processing."""

    entity: core.DataEntityBase
    definition_offset: int
    aliases: list[core.AccessPath]
    canonical_path: core.AccessPath | None = None
    entity_defn: LocalEntityDefn | None = None


_ENTITY_ID_TO_BUILDER: dict[int, tuple[FxtrCollectionBuilder, int]] = dict()
"""A mapping from Fxtr entity python object ID to the collection builders that created them and their definition offset within the collection, if it exists."""


_PYTHON_MODULE_BUILDERS: weakref.WeakKeyDictionary[types.ModuleType, FxtrCollectionBuilder] = (
    weakref.WeakKeyDictionary()
)
"""Builders associated with Python modules."""


def _lookup_builder(entity: core.AnyEntity) -> FxtrCollectionBuilder | None:
    maybe_pair = _ENTITY_ID_TO_BUILDER.get(id(entity))
    if maybe_pair is None:
        return None
    builder, _ = maybe_pair
    return builder


def _get_entity_key_for_foreign_entity(
    entity: core.AnyEntity, current_builder: FxtrCollectionBuilder
) -> ImportedEntityKey | UnknownEntityKey:
    """Get an entity key for a foreign entity (one not defined in the current builder).

    Args:
        entity: The foreign entity to get a key for.
        current_builder: The current collection builder.

    Returns:
        An ImportedEntityKey if the entity is from another collection, or an UnknownEntityKey
        if the entity's provenance is unknown.

    Raises:
        ValueError: If the entity is from an incomplete collection.
    """
    maybe_pair = _ENTITY_ID_TO_BUILDER.get(id(entity))
    if maybe_pair is not None:
        foreign_builder, foreign_offset = maybe_pair
        if foreign_builder is current_builder:
            shortrepr = repr(entity)[:100] + "..." if len(repr(entity)) > 100 else repr(entity)
            raise RuntimeError(
                "Entity was defined in this collection"
                f" ({current_builder._namespace_id}/{current_builder._name}) but not found! This"
                f" should be impossible! Entity was: {core.entity_cid(entity)} {shortrepr}"
            )
        if foreign_builder._collection is None:
            if foreign_builder._autoclose_for_module is not None:
                fmod, _ = foreign_builder._autoclose_for_module
                logger.debug(f"Autoclosing collection for {fmod.__name__} due to extraction request")
                foreign_builder.autoclose_module()
                assert foreign_builder._collection is not None
            else:
                shortrepr = repr(entity)[:100] + "..." if len(repr(entity)) > 100 else repr(entity)
                raise ValueError(
                    "While building collection"
                    f" {current_builder._namespace_id}/{current_builder._name}, found an entity"
                    " from another incomplete collection"
                    f" ({foreign_builder._namespace_id}/{foreign_builder._name}). This is not"
                    " supported! Make sure to finalize the other collection using `build(...)`"
                    " before depending on it in this collection. Entity was:"
                    f" {core.entity_cid(entity)} {shortrepr}"
                )
        return ImportedEntityKey(
            module=foreign_builder._collection,
            definition_offset=foreign_offset,
        )
    else:
        # Unknown entity; check if it's a builtin or a type with a default namespace ID
        if isinstance(
            entity, core.DataEntitySchema | core.InterfaceDefn | core.ExternalServiceIdentifier
        ) and entity.namespace_id.startswith("!builtin"):
            # Builtin entities are fine - they're always available (or, if they aren't, we'll raise
            # an error at upload time). We just treat them as "unknown" here.
            pass

        else:
            shortrepr = repr(entity)[:100] + "..." if len(repr(entity)) > 100 else repr(entity)
            warnings.warn(
                f"Found an entity with unknown provenance (type: {type(entity).__name__}, cid:"
                f" {core.entity_cid(entity)}) that was built outside of any collection builder."
                " Treating it as an unknown entity in collection"
                f" {current_builder._namespace_id}/{current_builder._name}. This may cause issues."
                f" Value: {shortrepr}"
            )

        return UnknownEntityKey(cid_b32=core.entity_cid(entity).encode(base="base32"))


def _walk_items_dict(items: Mapping[str, Any]) -> Iterable[entity_util.EntityAccessPath]:
    """Walk a dictionary of items and yield all entity access paths."""
    path: list[core.AccessPathStep] = []
    seen_ids: set[int] = set()

    def _walk(obj: Any) -> Iterable[entity_util.EntityAccessPath]:
        if id(obj) in seen_ids:
            return
        seen_ids.add(id(obj))
        if isinstance(obj, core.EntityBase):
            yield entity_util.EntityAccessPath(entity=obj, py_path=tuple(path), ipld_path=None)
        elif isinstance(obj, (dict, frozendict)):
            for key, value in obj.items():
                if isinstance(key, str):
                    path.append(core.AccessPathStep_GetItem_Str(key=key))
                elif isinstance(key, int):
                    path.append(core.AccessPathStep_GetItem_Int(key=key))
                elif isinstance(key, core.EntityBase):
                    path.append(core.AccessPathStep_GetItem_Entity(key=key))
                else:
                    continue
                yield from _walk(value)
                path.pop()
        elif isinstance(obj, (list, tuple)):
            for index, value in enumerate(obj):
                path.append(core.AccessPathStep_GetItem_Int(key=index))
                yield from _walk(value)
                path.pop()
        elif isinstance(obj, core.UserDefinedEntityMeta) and hasattr(obj, "value_typespec"):
            # This is an actual data entity or service identifier _class_ object.
            # We need to walk through its type or schema.
            match obj.value_typespec:
                case core.TypeSpec_DataEntity(schema=schema):
                    assert isinstance(schema, core.DataEntitySchema)
                    yield entity_util.EntityAccessPath(
                        entity=schema,
                        py_path=(
                            *path,
                            core.AccessPathStep_UserDefinedClass_ValueTypespec(),
                            core.AccessPathStep_GetAttr(attr_name="schema"),
                        ),
                        ipld_path=None,
                    )
                case _:
                    raise TypeError(f"Unknown value typespec {obj.value_typespec} at {path}")
        elif isinstance(obj, type) and issubclass(obj, core.FxtrCallTarget):
            if core.is_abstract_call_target(obj):
                yield entity_util.EntityAccessPath(
                    entity=core.interface_defn_of(obj),
                    py_path=(*path, core.AccessPathStep_InterfaceDefn()),
                    ipld_path=None,
                )
            else:
                # Not a registered interface, skip it with a warning
                warnings.warn(
                    f"Could not walk into non-abstract FxtrCallTarget {obj} at"
                    f" {core.access_path_to_str(root_str='', access_path=tuple(path))}"
                )
        else:
            warnings.warn(
                f"Could not walk into object of type {type(obj)} at"
                f" {core.access_path_to_str(root_str='', access_path=tuple(path))} (unknown type)"
            )
        seen_ids.remove(id(obj))

    # Walk each item in the dict
    for name, value in items.items():
        path.append(core.AccessPathStep_GetAttr(attr_name=name))
        yield from _walk(value)
        path.pop()


class FxtrCollectionBuilder:
    """A builder for Fxtr collections.

    This builder can be used to simplify the construction of Fxtr collections, by automatically
    tracking module dependencies through Python imports.
    """

    def __init__(
        self,
        namespace_id: str,
        name: str | None,
        autoclose_for_module: tuple[types.ModuleType, set[str]] | None = None,
    ):
        self._namespace_id = namespace_id
        self._name = name
        self._defined_entity_refs: list[weakref.ref[core.AnyEntity]] = []
        self._open: bool = False
        self._building: bool = False
        self._collection: Collection | None = None
        self._autoclose_for_module: tuple[types.ModuleType, set[str]] | None = autoclose_for_module

    def adopt_entity(self, entity: core.AnyEntity):
        """Adopt an entity.

        Args:
            entity: The entity to adopt.
        """
        if self._building:
            # Don't adopt entities that the builder is instantiating in `build` to track the
            # collection itself.
            return

        if not self._open:
            raise ValueError(
                "Collection not open! Entities can only be adopted during the build phase."
            )

        if id(entity) in _ENTITY_ID_TO_BUILDER:
            prev, _ = _ENTITY_ID_TO_BUILDER[id(entity)]
            if prev is not self:
                raise ValueError(
                    "Entity is already owned by another builder, cannot adopt it! Entity:"
                    f" {entity}. Owned by: {prev}"
                )
            return

        # Add the entity to the builder's reference list and the global map. When the entity is
        # garbage collected, remove it from the global map.
        _ENTITY_ID_TO_BUILDER[id(entity)] = (self, len(self._defined_entity_refs))
        self._defined_entity_refs.append(weakref.ref(entity))
        weakref.finalize(entity, _ENTITY_ID_TO_BUILDER.pop, id(entity))

        if isinstance(entity, core.Task):
            # Also retrieve and adopt the result handle.
            self.adopt_entity(entity.result_handle)

    def build(self, items: Mapping[str, NestedTrackable]) -> Collection:
        """Build a collection object.

        All entities that were defined in the scope of the builder and which still have Python
        references will be included in the collection. The subset that are reachable from `items`
        will be associated with those names in the collection.

        Args:
            items: The named items to include in the collection. Each value can be an entity, a
                subclass of DataEntityBase, a fxtr protocol definition, or a (frozen)dict, list, or
                tuple of such values. Note that other entities that were defined in the scope of
                the builder and which still have Python references will also be included in the
                collection, but will not have names.

            Returns:
                The collection object.
        """
        if not self._open:
            raise ValueError(
                "Collection not open! Please build the collection before exiting the context"
                " manager."
            )
        assert self._collection is None, "Collection already built!"

        # Extract alive entities from the refs
        alive_entities = []
        for ref in self._defined_entity_refs:
            entity = ref()
            if entity is not None:
                alive_entities.append(entity)

        # Immediately mark this collection as closed to catch any weird errors during build.
        self._open = False
        self._building = True

        # ============================================================================
        # Stage 1: Module-level alias collection & initialization
        # ============================================================================

        # Create alias state for all entities
        entity_alias_states: dict[int, _AliasState] = {}
        for i, entity in enumerate(alive_entities):
            entity_id = id(entity)
            entity_alias_states[entity_id] = _AliasState(
                entity=entity, definition_offset=i, aliases=[], canonical_path=None
            )

        # Also save re-exported root-level entities we discover during the walk
        re_exported_entities: list[ReexportedEntityInfo] = []

        # Walk the items dict to collect initial aliases
        items_access_paths = list(_walk_items_dict(items))

        for access_path in items_access_paths:
            obj_id = id(access_path.entity)

            if obj_id in entity_alias_states:
                # This is an entity that was defined in this collection.
                entity_alias_states[obj_id].aliases.append(access_path.py_path)
            else:
                # Foreign entity - get its key
                entity_key = _get_entity_key_for_foreign_entity(access_path.entity, self)
                re_exported_entities.append(
                    ReexportedEntityInfo(py_path=access_path.py_path, entity_key=entity_key)
                )

        # Initial canonicalization: if exactly 1 alias → set as canonical_path
        for state in entity_alias_states.values():
            if len(state.aliases) == 1:
                state.canonical_path = state.aliases[0]

        # ============================================================================
        # Stage 2: Single reverse walk for entity processing
        # ============================================================================

        # Process entities in REVERSE definition order
        for entity in reversed(alive_entities):
            entity_id = id(entity)
            state = entity_alias_states[entity_id]

            # Before processing: canonical path assignment with tie-breaking
            if state.canonical_path is None:
                if len(state.aliases) == 1:
                    # Single alias → promote to canonical
                    state.canonical_path = state.aliases[0]
                elif len(state.aliases) > 1:
                    # Multiple aliases → apply tie-breaking based on complexity
                    def count_complex_steps(path: core.AccessPath) -> int:
                        """Count path steps that are NOT simple (attr, item_str, item_int)."""
                        complex_count = 0
                        for step in path:
                            if not isinstance(
                                step,
                                (
                                    core.AccessPathStep_GetAttr,
                                    core.AccessPathStep_GetItem_Str,
                                    core.AccessPathStep_GetItem_Int,
                                ),
                            ):
                                complex_count += 1
                        return complex_count

                    # Find minimum complexity and check if it's unique
                    complexities = [(count_complex_steps(alias), alias) for alias in state.aliases]
                    min_complexity = min(complexity for complexity, _ in complexities)
                    simplest_aliases = [
                        alias for complexity, alias in complexities if complexity == min_complexity
                    ]

                    if len(simplest_aliases) == 1:
                        # Clear winner → promote to canonical
                        state.canonical_path = simplest_aliases[0]
                    # else: tie remains, leave canonical_path as None

            # Walk entity contents to find children
            child_entity_keys = []
            child_collector = entity_util.EntityChildCollector()
            entity_spec = core.typespec(entity)
            entity_spec.traverse_value(entity, child_collector)

            typespec_child_collector = entity_util.EntityChildCollector()
            entity_spec.traverse_self(typespec_child_collector)

            # Process visited entities in each category
            for visitor, py_prefix, ipld_prefix in [
                (child_collector, (), ()),
                (
                    typespec_child_collector,
                    (core.AccessPathStep_EntityTypespec(),),
                    (core.AccessPathStep_GetItem_Str("$typespec"),),
                ),
            ]:
                for visit_info in visitor.visited_entities:
                    child_id = id(visit_info.entity)
                    if child_id in entity_alias_states:
                        # Child is local to this collection
                        child_state = entity_alias_states[child_id]
                        child_key = LocalEntityKey(definition_offset=child_state.definition_offset)
                        if state.canonical_path is not None:
                            # Tell the child about its new alias that goes through this entity
                            child_state.aliases.append(
                                state.canonical_path + py_prefix + visit_info.py_path
                            )
                    else:
                        # Foreign entity - get its key
                        child_key = _get_entity_key_for_foreign_entity(visit_info.entity, self)

                    child_entity_keys.append(
                        EntityAtPathInfo(
                            py_path=py_prefix + visit_info.py_path,
                            ipld_path=ipld_prefix + visit_info.ipld_path,
                            entity_key=child_key,
                        )
                    )

            # Extract documentation metadata for recognized entities
            match entity:
                case core.DataEntitySchema(_doc=doc):
                    documentation = doc
                case _:
                    documentation = None

            # Create the final entity defn
            entity_defn = LocalEntityDefn(
                definition_offset=state.definition_offset,
                entity=entity,
                canonical_access_path=core.freeze(
                    state.canonical_path, freeze_hook=core.call_to_ipld
                ),
                aliases=core.freeze(state.aliases, freeze_hook=core.call_to_ipld),
                child_entities=core.freeze(child_entity_keys, freeze_hook=core.call_to_ipld),
                documentation=documentation,
            )
            state.entity_defn = entity_defn

            # If this is a task with a canonical path, update the result_handle's canonical path.
            # TODO(daniel): This is somewhat brittle; we should find a better way to assign
            # canonical names to derived entities.
            if isinstance(entity, core.Task) and state.canonical_path is not None:
                result_handle = entity.result_handle
                result_handle_id = id(result_handle)
                assert (
                    result_handle_id in entity_alias_states
                ), f"Task {entity} has a canonical path but its result_handle is not tracked"
                result_handle_state = entity_alias_states[result_handle_id]
                assert result_handle_state.entity_defn is not None, (
                    f"Task {entity} has a canonical path but its result_handle does not yet have "
                    "an entity_defn (should have been processed earlier in reverse walk)"
                )
                # Update the result_handle's canonical path
                result_handle_state.canonical_path = state.canonical_path + (
                    core.AccessPathStep_GetAttr(attr_name="result_handle"),
                )
                # Recreate the entity_defn with the updated canonical path
                result_handle_state.entity_defn = LocalEntityDefn(
                    definition_offset=result_handle_state.definition_offset,
                    entity=result_handle_state.entity,
                    canonical_access_path=core.freeze(
                        result_handle_state.canonical_path, freeze_hook=core.call_to_ipld
                    ),
                    aliases=result_handle_state.entity_defn.aliases,
                    child_entities=result_handle_state.entity_defn.child_entities,
                    documentation=result_handle_state.entity_defn.documentation,
                )

        # Collect entity_defns in original definition order
        entity_defns = []
        for entity in alive_entities:
            defn = entity_alias_states[id(entity)].entity_defn
            assert defn is not None, f"Entity {entity} has no entity_defn!"
            entity_defns.append(defn)

        # ============================================================================
        # Stage 3: Infer dependencies from foreign entities
        # ============================================================================

        dependencies: set[Collection] = set()
        for entity in alive_entities:
            # Walk the entity to find all foreign entities
            entity_spec = core.typespec(entity)
            collector = dag_util.RecursiveSubgraphCollector(
                include_if=lambda ent: id(ent) in entity_alias_states
            )
            collector.visit_entity(entity)
            entity_spec.traverse_self(collector)

            # Check excluded entities for dependencies
            for foreign_entity in collector.excluded_entities:
                maybe_pair = _ENTITY_ID_TO_BUILDER.get(id(foreign_entity))
                if maybe_pair is not None:
                    foreign_builder, _ = maybe_pair
                    if foreign_builder is not self and foreign_builder._collection is not None:
                        dependencies.add(foreign_builder._collection)

        # ============================================================================
        # Stage 4: Final assembly
        # ============================================================================

        self._collection = Collection(
            namespace_id=self._namespace_id,
            name=self._name,
            dependencies=frozenset(dependencies),
            entity_defns=tuple(entity_defns),
            re_exported_entities=core.freeze(re_exported_entities, freeze_hook=core.call_to_ipld),
        )
        self._building = False
        return self._collection

    def build_for_module(self, module: types.ModuleType, existing_names_at_start: set[str] = set()):
        """Build the collection for an associated module, retrieving values from the module."""
        # Walk the module's namespace.
        if "__all__" in module.__dict__:
            all_names = module.__dict__["__all__"]
        else:
            all_names = [
                key
                for key, value in module.__dict__.items()
                if (
                    not key.startswith("_")
                    and not isinstance(value, types.ModuleType)
                    and key not in existing_names_at_start
                )
            ]
        # Exclude functions
        data_names = [
            name for name in all_names if not isinstance(module.__dict__[name], types.FunctionType)
        ]
        items = {name: module.__dict__[name] for name in data_names}
        return self.build(items)

    def autoclose_module(self):
        assert self._autoclose_for_module is not None
        module, existing_names_at_start = self._autoclose_for_module
        if core._ENTITY_CREATION_STATE.is_tracked_module_executing(module.__name__):
            raise RuntimeError(
                "Tried to extract a fxtr collection from a Python module that is still being"
                " imported! Depending on entities defined in an incomplete collection module is not"
                " supported; make sure the module finishes executing first."
            )
        self.build_for_module(module, existing_names_at_start)


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

    prev_state = core._ENTITY_CREATION_STATE.get()
    if prev_state is not None:
        prev_creation_hook = prev_state.creation_hook
    else:
        prev_creation_hook = None

    def _collection_creation_hook(entity: core.AnyEntity):
        if prev_creation_hook is not None:
            prev_creation_hook(entity)

        builder.adopt_entity(entity)

    new_state = core.EntityCreationState(
        creation_hook=_collection_creation_hook, default_namespace_id=namespace_id
    )
    with core._ENTITY_CREATION_STATE.locally_scoped_assign(new_state):
        builder._open = True
        yield builder
        if builder._collection is None:
            raise ValueError(
                "Collection not built! Please call `build` on the builder before exiting the"
                " context manager."
            )
        assert not builder._open, "Builder should be closed by now!"


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

    If the module defines `__all__`, only entities in that list will be included in the collection.
    Otherwise, all non-private, non-function entities in the module that were NOT in the module
    when the context manager was entered will be included. (This is a heuristic to avoid
    re-exporting imported modules and types.) Note that even if __all__ is defined, functions will
    always be excluded, to make it easier to define functions alongside entities.)

    Args:
        module_name: The name of the current Python module. Should always be set to __name__ by the
            caller.
        namespace_id: The namespace of the collection.
        export_as: The name of the collection. If not provided, the collection will be named equal
            to the module name.
    """
    if module_name not in sys.modules:
        raise ValueError(f"Module {module_name} is not a module in sys.modules!")

    module = sys.modules[module_name]

    if module in _PYTHON_MODULE_BUILDERS:
        raise ValueError(f"Module {module.__name__} is already a collection module!")

    existing_names_at_start = set(module.__dict__.keys())
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
        _PYTHON_MODULE_BUILDERS[module] = builder
        yield builder
        if builder._collection is not None:
            raise ValueError(
                "Collections should not be built manually when using `define_collection_module`; it"
                " will be built automatically at contextmanager exit."
            )
        builder.build_for_module(module, existing_names_at_start=existing_names_at_start)


def declare_collection_module(
    *,
    namespace_id: str,
    export_as: str | None | _UseModuleName = _UseModuleName(),
) -> None:
    """Declare a module as a collection module with a default namespace.

    This function should be called at the top level of a module to register the module's
    namespace ID. All entities created in this module after calling `declare_collection_module`
    will be associated with a collection corresponding to this module.

    Args:
        namespace_id: The namespace of the collection.
        export_as: The name of the collection. If not provided, the collection will be named equal
            to the module name.

    Raises:
        ValueError: If the module is not a module in sys.modules, or if it already has a default
            collection namespace registered via `register_module_namespace`.
    """
    module_name = modulescoping.find_module_name_from_frame_at_stacklevel(2)
    if module_name is None:
        raise RuntimeError(
            "Could not identify the name of the Python module that is currently executing!"
            " `declare_collection_module` must be called at the top level of a module."
        )

    if module_name not in sys.modules:
        raise RuntimeError(f"Module {module_name} is not a module in sys.modules!")
    module = sys.modules[module_name]

    if module in _PYTHON_MODULE_BUILDERS:
        raise ValueError(
            f"Module {module_name} is already a collection module (via `define_collection_module`)!"
        )

    existing_names_at_start = set(module.__dict__.keys())
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

    builder = FxtrCollectionBuilder(
        namespace_id, collection_name, autoclose_for_module=(module, existing_names_at_start)
    )
    builder._open = True
    _PYTHON_MODULE_BUILDERS[module] = builder

    # Set this builder as active at module level. We will finalize it the first time we try to
    # access the collection from the module.
    creation_state = core.EntityCreationState(
        creation_hook=builder.adopt_entity, default_namespace_id=namespace_id
    )
    core._ENTITY_CREATION_STATE.module_level_assign(module_name, creation_state)


def extract_collection_from_module(module: types.ModuleType) -> Collection:
    """Get the collection module for a given module (by name)"""
    if module not in _PYTHON_MODULE_BUILDERS:
        raise ValueError(
            f"Module {module.__name__} does not have an explicit fxtr collection module!"
        )
    builder = _PYTHON_MODULE_BUILDERS[module]
    if not builder._collection:
        if builder._autoclose_for_module is not None:
            logger.debug(f"Autoclosing collection for {module.__name__} due to extraction request")
            builder.autoclose_module()
            assert builder._collection is not None
            return builder._collection
        else:
            raise ValueError(
                f"Module {module.__name__}'s collection builder is still executing! Could not"
                " extract collection."
            )
    return builder._collection


# endregion
####################################################################################################
# region: Fixups to core builtins
####################################################################################################

with build_collection("!builtin", "!builtin") as _builtin_builder:
    _builtin_builder.adopt_entity(core.interface_defn_of(core.TaskFunction))
    _builtin_builder.adopt_entity(core.interface_defn_of(core.StorageHandle))
    _builtin_builder.adopt_entity(core.interface_defn_of(core.RuntimeEnvRetriever))
    _builtin_builder.adopt_entity(core.interface_defn_of(core.SecretRetriever))
    _builtin_builder.build(
        frozendict(
            TaskFunction=core.TaskFunction,
            StorageHandle=core.StorageHandle,
            RuntimeEnvRetriever=core.RuntimeEnvRetriever,
            SecretRetriever=core.SecretRetriever,
            SecretFromEnvVarDeclaration=core.SecretFromEnvVarDeclaration,
            SecretFromOptionalEnvVarDeclaration=core.SecretFromOptionalEnvVarDeclaration,
        )
    )


# endregion
####################################################################################################
