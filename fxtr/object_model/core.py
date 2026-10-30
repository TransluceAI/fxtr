"""Core object representation for the hub entity system.

The hub entity system is built on the IPLD data model, and in particular the DAG-CBOR codec. This
is used as the serialization format for various objects that are stored in the hub.

There are three main categories of values in the hub entity system:
- Values: Immutable objects containing data, that we want to be able to pass around and serialize
  to/from IPLD.
- Type-specs: Objects describing a set of values, and also describing how to serialize them to/from
  IPLD.
- Entities: Self-describing values have two serialized forms: (1) their contents, which can then be
  hashed to produce a content-address, and which must always include their own type-spec, and (2)
  a content-addressed link to their contents, used when an entity appears inside another value.

To enable maximum flexibility and interoperability of tooling around the object model, we embed
one level of reflection into the system: every type-spec is itself a value in the system, described
by a meta type-spec (which also describes itself). Additionally, type-specs can contain arbitrary
values, including in particular references to entities.

This makes it possible to bootstrap into user-defined types from a small set of primitives. An
incomplete list:

TypeSpec_TypeSpec: The type-spec for type-specs, which describes a _hardcoded_ (but
system-level-extensible) set of type-specs:
- TypeSpec_Int, TypeSpec_Str, etc: Primitives and builtins.
- TypeSpec_List, TypeSpec_Dict, etc: Immutable collections.
- TypeSpec_TypeSpec: describing itself, so that it can serialize itself.
- TypeSpec_DataEntitySchema: describing an entity type that contains a schema for a
    user-defined entity.
- TypeSpec_DataEntity: describing any user-defined entity, which references its
    DataEntityTypeEntity.

Thus a value of a user-defined type points to the user-defined definition of the type, which points
to the hardcoded type-spec for user-defined types, which points to the hardcoded type-spec for
type specs, which points to itself. At this point everything is self-contained and we can serialize
this whole object to an IPLD DAG.

TODO(daniel): Currently, objects like "DataEntitySchema" and "InterfaceSpec" have string
documentation defining the associated contract for using them. However, this contract is not
actually stored in the content-hash of the schema/spec entity. Instead, there is a namespace tag,
and we assume that such schemas/specs are referred to "outside the system" by e.g. looking at the
docstring pushed to the hub by the owner of the namespace. This gives flexibility to change the
docstring to add notes or clarifications without changing the content-hash of the schema/spec
entity. However, it is unclear if this is the right way to go. An alternative would be to define
the contract for such entities directly inside the entity, such that any change to the contract is
actually a breaking change for all data/implementations that use that schema (by virtue of changing
their content-hashes). We should consider the tradeoffs here and possibly migrate to this system
in the future. (In practice the two versions could co-exist if we create a new version of the entity
that represents the schema/spec, and refer to it in new definitions.)
"""

from __future__ import annotations

import abc
import collections.abc
import contextlib
import contextvars
import dataclasses
import functools
import inspect
import logging
import sys
import types
import typing
import warnings
import weakref
from types import NotImplementedType
from typing import (
    Annotated,
    Any,
    Callable,
    ClassVar,
    Collection,
    Generator,
    Iterator,
    Literal,
    Mapping,
    Protocol,
    Self,
    Sequence,
    TypeGuard,
)

import cbrrr
from frozendict import frozendict

from fxtr.object_model import cbor_util, modulescoping

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Exception raised when a value is not valid for a type."""


class TypeAnnotationConversionError(Exception):
    """Exception raised when a Python type annotation cannot be converted to/from a hub type spec."""


####################################################################################################
# region: Protocol definitions for entity handling
####################################################################################################


@dataclasses.dataclass
class EntityCreationState:
    creation_hook: Callable[[EntityBase], None] | None = None
    default_namespace_id: str | None = None


_ENTITY_CREATION_STATE: modulescoping.ModuleScopedAssignment[EntityCreationState] = (
    modulescoping.ModuleScopedAssignment.create("_ENTITY_CREATION_DATA")
)
"""Current entity creation hooks and namespace id, if present."""


class HubValueVisitor(Protocol):
    """Protocol for visitors of hub values."""

    def visit_part_at_path(
        self, *, py_path: AccessPath, ipld_path: AccessPath, spec: TypeSpecBase, value: Any
    ):
        """Visit a part of a value at a given path.

        Args:
            py_path: The path of how to access the object in the Python value.
            ipld_path: The path of how to access the object after IPLD/DAG-CBOR conversion.
            spec: The typespec of the object.
            value: The object to visit.
        """
        ...


class RecursiveEntityVisitor(HubValueVisitor):
    """Base visitor implementation that visits entities, recursing into other types.

    Meant to be subclassed and extended to handle traversal logic.
    """

    def __init__(self):
        self._py_path = []
        self._ipld_path = []

    def visit_entity(
        self, *, py_path_from_root: AccessPath, ipld_path_from_root: AccessPath, entity: EntityBase
    ):
        """Visit an entity."""

    def visit_part_at_path(
        self, *, py_path: AccessPath, ipld_path: AccessPath, spec: TypeSpecBase, value: Any
    ):
        """Visit a part of a value at a given path."""
        self._py_path.extend(py_path)
        self._ipld_path.extend(ipld_path)
        try:
            if isinstance(value, EntityBase):
                # We don't pass the spec to the visit_entity callback because it is always possible
                # to infer the actual spec from the entity.
                self.visit_entity(
                    py_path_from_root=tuple(self._py_path),
                    ipld_path_from_root=tuple(self._ipld_path),
                    entity=value,
                )
            else:
                # TODO: special cases for user-defined types, forward references?
                spec.traverse_value(value, self)
        finally:
            for _ in py_path:
                self._py_path.pop()
            for _ in ipld_path:
                self._ipld_path.pop()


class EntityLookup(Protocol):
    """Protocol for looking up entities by their CID."""

    def __call__(self, cid: cbrrr.CID) -> EntityBase:
        """Lookup an entity by its CID."""
        ...


# endregion
####################################################################################################
# region: Helper types
####################################################################################################


@dataclasses.dataclass(frozen=True)
class EntityForwardRefMetadata:
    """Metadata for forward reference type annotations containing namespace and type_id."""

    namespace_id: str
    type_id: str


def entity_type_fwdref(type_id: str, *, namespace_id: str | None = None) -> Any:
    """Create a forward reference type annotation for a data entity.

    Args:
        type_id: The type identifier for the entity type.
        namespace_id: The namespace identifier for the entity type. Optional when used inside a
            blueprint module, but required at the core level.

    Returns:
        An Annotated type that represents a forward reference to the specified entity type.

    Example:
        PersonRef = entity_type_fwdref("Person^1", namespace_id="myapp")
        # This can be used in recursive type definitions
    """
    namespace_id = _get_type_namespace_id(namespace_id)
    return Annotated[
        DataEntityBase, EntityForwardRefMetadata(namespace_id=namespace_id, type_id=type_id)
    ]


@dataclasses.dataclass(frozen=True)
class HasTypeSpec:
    """Metadata for Python types that have a typespec.

    Can be used in Python to make a type annotation with a given typespec, via

        Annotated[Any, HasTypeSpec(typespec=...)]
    """

    typespec: TypeSpecBase


type CID_Of[T: EntityBase] = cbrrr.CID
"""Type annotation to indicate that a CID should be interpreted as a CID of the given entity type.

Currently not part of any entity schema, used only for documentation purposes.
"""

# endregion
####################################################################################################
# region: Entity base
####################################################################################################


class EntityBase(abc.ABC):
    """Base class for Python classes representing entities in the object model.

    Every Python entity class is expected to have attributes describing how it can be represented
    in the hub.

    Subclasses must define the attributes below. They _also_ must call `_notify_entity_created`
    in their `__init__` or `__post_init__` method.

    Attributes:
        _fxtr_cid: The CID of the entity, computed by serializing the entity to DAG-CBOR and computing
            a sha2-256 hash of the result.
        _fxtr_typespec: The type-spec for the entity, describing how it can be serialized to/from IPLD
            and how to traverse it.
    """

    _fxtr_cid: cbrrr.CID
    _fxtr_typespec: TypeSpecBase_SomeEntity

    def __hash__(self) -> int:
        return hash(self._fxtr_cid)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, EntityBase):
            return self._fxtr_cid == other._fxtr_cid
        return False


# Type alias for better readability when storing arbitrary entities
AnyEntity = EntityBase


def typespec(entity: EntityBase) -> TypeSpecBase_SomeEntity:
    """Get the type-spec for an entity."""
    return entity._fxtr_typespec


def entity_cid(entity: EntityBase) -> cbrrr.CID:
    """Get the CID of an entity.

    Assumes that the entity has already been created and has a CID. Not safe to call during entity
    construction.
    """
    return entity._fxtr_cid


def _compute_entity_cid(entity: EntityBase) -> cbrrr.CID:
    """Compute the CID of an entity.

    Does not assume that the _fxtr_cid has yet been populated.
    """
    ipld_data = entity._fxtr_typespec.entity_data_to_ipld(entity)
    cid_bytes = cbor_util.encode_cbor(ipld_data)
    return cbrrr.CID.cidv1_dag_cbor_sha256_32_from(cid_bytes)


def _notify_entity_created(entity: EntityBase) -> None:
    """Notify the active entity creation hook that an entity has been created.

    This _must_ be called by any subclass of EntityBase in its `__init__` or `__post_init__` method.
    """
    entity_creation_data = _ENTITY_CREATION_STATE.get()
    if entity_creation_data is not None and entity_creation_data.creation_hook is not None:
        entity_creation_data.creation_hook(entity)


# endregion
####################################################################################################
# region: AnyFrozenIPLDable
####################################################################################################

"""Any JSON-like structure that can be one-to-one mapped to IPLD DAG-CBOR."""
AnyFrozenIPLDable = (
    str
    | bytes
    | int
    | bool
    | float
    | None
    | frozendict[str, "AnyFrozenIPLDable"]
    | tuple["AnyFrozenIPLDable", ...]
    | EntityBase
)

AnyIPLDable = AnyFrozenIPLDable | Mapping[str, "AnyIPLDable"] | Sequence["AnyIPLDable"]


def freeze(
    value: Any,
    freeze_hook: Callable[[Any], AnyFrozenIPLDable | NotImplementedType] | None = None,
):
    """Freeze a value if possible.

    Args:
        value: The value to freeze.
        freeze_hook: A hook that can be used to enable freezing for otherwise unsupported types.
            Can return `NotImplemented` to indicate that we don't know how to freeze this type.

    Returns:
        A frozen version of this value, which replaces all mappings with frozendicts and all
        sequences with tuples.
    """
    if isinstance(value, str | int | float | bool | bytes | None | EntityBase):
        return value
    elif isinstance(value, Mapping):
        return frozendict({k: freeze(v, freeze_hook=freeze_hook) for k, v in value.items()})
    elif isinstance(value, Sequence):
        return tuple(freeze(v, freeze_hook=freeze_hook) for v in value)
    else:
        if freeze_hook is not None:
            result = freeze_hook(value)
            if result is not NotImplemented:
                return result
            else:
                logger.debug(f"Tried and failed to freeze {value} with {freeze_hook}")
        raise ValueError(f"Cannot freeze value of type {type(value)}")


def call_to_ipld(obj: Any) -> AnyFrozenIPLDable | NotImplementedType:
    """Freeze hook that calls to_ipld on the object if it has it."""
    if hasattr(obj, "to_ipld"):
        return freeze(obj.to_ipld())
    else:
        return NotImplemented


# endregion
####################################################################################################
# region: Hub type specifications
####################################################################################################


class TypeSpecBase(abc.ABC):
    """Base class for all hub type specifications.

    Hub type specifications are objects that represent a category of values in the hub entity system.
    For the purposes of the hub, a type is a set of values. We do NOT assume you can infer a
    type from an object; they must come from a type definition.

    Restrictions:
    - we must be able to validate values against it to make sure they have this type
    - if a value belongs to this type:
        - it must be immutable
        - we must know how to (de)serialize it to/from IPLD, and this must perfectly reconstruct
          any such value (as long as you know the type spec during both the serialization and
          deserialization).
        - we must know how to traverse it with a visitor
        - we should be able to do all these things without ever running into an error except at
          validation time; this means we do not allow foreign objects we don't understand at all.
    - this type must also be able to serialize/deserialize itself to/from IPLD (which we use to
      implement the "meta-level" TypeSpec_TypeSpec type spec) and must have a unique "kind"
      string (among TypeSpecs in the system, registered with TypeSpec_TypeSpec).

    As a design choice, we also follow these additional constraints:
    - validating a value against one of these types must be O(v + t) where v is the size of the
      value and t is the size of the type; in particular this means we should not need to backtrack
      through union choices, and should instead be able to immediately decide whether a given
      value matches a given union variant in constant time.
    - it must be possible to determine whether one type includes another (not currently used, but
      may be useful for typechecking and linting experiment definitions)
    """

    @abc.abstractmethod
    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        ...

    @abc.abstractmethod
    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        ...

    # Value-level operations (used directly)

    @abc.abstractmethod
    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse the value with the given visitor.

        Traversal must recursively visit all child values contained within the value, along with
        their typespecs (as tracked by this visitor).
        """
        ...

    @abc.abstractmethod
    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        """Converts a value of this type to IPLD object model representation.

        Hard constraint:
        - The IPLD representation must be identical across reruns for the same input.
        - Any change to the IPLD format of an existing object is a breaking change.

        Args:
            value: The value to convert. As a precondition, this value must be known to be valid
                for this type.

        Returns:
            An IPLD object model representation that can be encoded to DAG-CBOR.
        """
        ...

    @abc.abstractmethod
    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        """Reconstructs a value of this type from IPLD object model representation.

        Args:
            ipld_value: An IPLD object model representation (from DAG-CBOR decoding).
            lookup: Object that can load entities by their CID.

        Returns:
            A value described by this type spec.
        """
        ...

    # Type-level operations (called by TypeSpec_TypeSpec to implement the value-level operations)

    @abc.abstractmethod
    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse the type spec itself with the given visitor."""
        ...

    @abc.abstractmethod
    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Converts the type spec itself to IPLD object model representation."""
        ...

    @classmethod
    @abc.abstractmethod
    def ipld_to_this_type(cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Self:
        """Reconstructs an instance of this type spec from its IPLD object model representation."""
        ...


class TypeSpecBase_SomeEntity(TypeSpecBase):
    """Base class for type specs that describe an entity type.

    In compound structures, entity objects always serialize as links to CIDs describing their
    contents, and their types serialize to links to their type specifications. To work with the
    actual representation of the entity's data, you can use `entity_data_to_ipld` and
    `ipld_to_entity_object`.

    Traversing a value that is an entity should traverse its contents, but does not need to traverse
    its type spec. Visitors that want to handle entities in a special way can customize this by
    - interrupting a visit when encountering an entity, and possibly
    - directly extracting and visiting the typespec using the entity's "_fxtr_typespec" attribute.
    """

    # New abstract methods

    @abc.abstractmethod
    def entity_data_to_ipld(self, entity: EntityBase) -> cbrrr.DagCborTypes:
        """Converts the data of an entity of this type to IPLD object model representation.

        Serialized entity data _must_ always have a top-level key "$typespec" that contains the
        serialized TypeSpecBase_SomeEntity itself.
        """
        ...

    @abc.abstractmethod
    def ipld_to_entity_object(
        self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> EntityBase:
        """Reconstructs an entity of this type from the IPLD representation of its data.

        Note that this method will be called _after_ deserializing the type spec from the $typespec
        key, so the type spec does not need to be checked here.
        """
        ...

    # Implementations of value-level operations that are shared for all entities

    def value_to_ipld(self, value: EntityBase) -> cbrrr.DagCborTypes:
        """Entity values always serialize as CID links in compound structures."""
        return value._fxtr_cid

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> EntityBase:
        """Reconstruct entity value by looking up the CID link."""
        if not isinstance(ipld_value, cbrrr.CID):
            raise ValidationError(
                f"Expected CID for entity deserialization, got {type(ipld_value)}"
            )
        return lookup(ipld_value)


# endregion
####################################################################################################
# region: Primitive type specifications
####################################################################################################


@dataclasses.dataclass(frozen=True)
class TypeSpec_Primitive(TypeSpecBase):
    """A specification for Python primitive values that can be serialized as-is."""

    primitive_type: type[int | float | str | bool | None]

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        return isinstance(other, TypeSpec_Primitive) and self.primitive_type == other.primitive_type

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if type(value) is not self.primitive_type:
            raise ValidationError(
                f"Expected a value of type {self.primitive_type.__name__}, got {repr(value)} of"
                f" type {type(value).__name__}"
            )

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Primitive values have no sub-structure to traverse."""

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        return value

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        self.validate(ipld_value)
        return ipld_value

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Primitive type specs have no sub-structure to traverse."""

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        if self.primitive_type is int:
            return {"$kind": "int"}
        elif self.primitive_type is float:
            return {"$kind": "float"}
        elif self.primitive_type is str:
            return {"$kind": "str"}
        elif self.primitive_type is bool:
            return {"$kind": "bool"}
        elif self.primitive_type is type(None):
            return {"$kind": "none"}
        else:
            raise ValueError(f"Unknown primitive type: {self.primitive_type}")

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_Primitive:
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        kind = ipld_value["$kind"]

        if kind == "int":
            primitive_type = int
        elif kind == "float":
            primitive_type = float
        elif kind == "str":
            primitive_type = str
        elif kind == "bool":
            primitive_type = bool
        elif kind == "none":
            primitive_type = type(None)
        else:
            raise ValueError(f"Unknown primitive kind: {kind}")

        return cls(primitive_type=primitive_type)


@dataclasses.dataclass(frozen=True)
class TypeSpec_ExternalCID(TypeSpecBase):
    """A specification for external CID values that reference objects not currently present.

    External CIDs serialize as base32-encoded CID strings. We do this so that, in DAG-CBOR, they
    are NOT confusable with entity links. This means you can find the set of entities referenced
    by another entity by looking for the actual CID links.

    This matches the semantics of the way the entities are actually used: we never follow external
    CID links when decoding entities, since they are "just data".
    """

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        return isinstance(other, TypeSpec_ExternalCID)

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if not isinstance(value, cbrrr.CID):
            raise ValidationError(f"Expected cbrrr.CID, got {type(value).__name__}")

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """External CIDs have no sub-structure to traverse."""

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        """External CIDs serialize directly as the CID."""
        return value.encode("base32")

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> cbrrr.CID:
        """Reconstruct external CID from IPLD representation."""
        if not isinstance(ipld_value, str):
            raise ValidationError(
                f"Expected string for external CID deserialization, got {type(ipld_value)}"
            )
        return cbrrr.CID.decode(ipld_value)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """External CID type specs have no sub-structure to traverse."""

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert the type spec itself to IPLD."""
        return {"$kind": "external_cid"}

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_ExternalCID:
        """Reconstruct type spec from IPLD."""
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "external_cid", f"Expected external_cid kind"
        return cls()


# endregion
####################################################################################################
# region: Collection type specifications
####################################################################################################


@dataclasses.dataclass(frozen=True)
class TypeSpec_FixedSizeTuple(TypeSpecBase):
    """A specification for fixed-size tuples with specific element types."""

    item_types: tuple[TypeSpecBase, ...]

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        return (
            isinstance(other, TypeSpec_FixedSizeTuple)
            and len(self.item_types) == len(other.item_types)
            and all(
                my_type.is_supertype_of(other_type)
                for my_type, other_type in zip(self.item_types, other.item_types)
            )
        )

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if not isinstance(value, tuple):
            raise ValidationError(f"Expected tuple, got {type(value).__name__}")

        if len(value) != len(self.item_types):
            raise ValidationError(
                f"Expected tuple of length {len(self.item_types)}, got length {len(value)}"
            )

        for i, (item, item_type) in enumerate(zip(value, self.item_types)):
            try:
                item_type.validate(item)
            except ValidationError as e:
                e.add_note(f"In item {i}")
                raise

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse all tuple items."""
        for i, (item, item_type) in enumerate(zip(value, self.item_types)):
            visitor.visit_part_at_path(
                py_path=(AccessPathStep_GetItem_Int(key=i),),
                ipld_path=(AccessPathStep_GetItem_Int(key=i),),
                spec=item_type,
                value=item,
            )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        result = []
        for item, item_type in zip(value, self.item_types):
            result.append(item_type.value_to_ipld(item))
        return result

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        if not isinstance(ipld_value, list):
            raise ValidationError(
                f"Expected list for tuple deserialization, got {type(ipld_value).__name__}"
            )

        if len(ipld_value) != len(self.item_types):
            raise ValidationError(f"Expected {len(self.item_types)} items, got {len(ipld_value)}")

        result_items = []
        for item_data, item_type in zip(ipld_value, self.item_types):
            result_items.append(item_type.ipld_to_value(item_data, lookup))

        return tuple(result_items)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse all item types."""
        for i, item_type in enumerate(self.item_types):
            visitor.visit_part_at_path(
                py_path=(
                    AccessPathStep_GetAttr(attr_name="item_types"),
                    AccessPathStep_GetItem_Int(key=i),
                ),
                ipld_path=(
                    AccessPathStep_GetItem_Str(key="item_types"),
                    AccessPathStep_GetItem_Int(key=i),
                ),
                spec=TypeSpec_TypeSpec(),
                value=item_type,
            )

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        item_types_list = []
        for item_type in self.item_types:
            item_types_list.append(item_type.self_to_ipld())
        return {"$kind": "fixed_tuple", "item_types": item_types_list}

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_FixedSizeTuple:
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "fixed_tuple", f"Expected fixed_tuple kind"

        item_types_data = ipld_value["item_types"]
        assert isinstance(item_types_data, list), f"Expected list for item_types"

        item_types = []
        for item_type_data in item_types_data:
            item_types.append(ipld_to_type_spec(item_type_data, lookup))

        return cls(item_types=tuple(item_types))


@dataclasses.dataclass(frozen=True)
class TypeSpec_VariableSizeTuple(TypeSpecBase):
    """A specification for variable-size tuples with homogeneous element types."""

    repeated_type: TypeSpecBase

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        if isinstance(other, TypeSpec_VariableSizeTuple):
            return self.repeated_type.is_supertype_of(other.repeated_type)
        elif isinstance(other, TypeSpec_FixedSizeTuple):
            return all(
                self.repeated_type.is_supertype_of(item_type) for item_type in other.item_types
            )
        else:
            return False

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if not isinstance(value, tuple):
            raise ValidationError(f"Expected tuple, got {type(value).__name__}")

        for i, item in enumerate(value):
            try:
                self.repeated_type.validate(item)
            except ValidationError as e:
                e.add_note(f"In item {i}")
                raise

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse all tuple items."""
        for i, item in enumerate(value):
            visitor.visit_part_at_path(
                py_path=(AccessPathStep_GetItem_Int(key=i),),
                ipld_path=(AccessPathStep_GetItem_Int(key=i),),
                spec=self.repeated_type,
                value=item,
            )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        result = []
        for item in value:
            result.append(self.repeated_type.value_to_ipld(item))
        return result

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        if not isinstance(ipld_value, list):
            raise ValidationError(
                f"Expected list for tuple deserialization, got {type(ipld_value).__name__}"
            )

        result_items = []
        for item_data in ipld_value:
            result_items.append(self.repeated_type.ipld_to_value(item_data, lookup))

        return tuple(result_items)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse the repeated type."""
        visitor.visit_part_at_path(
            py_path=(AccessPathStep_GetAttr(attr_name="repeated_type"),),
            ipld_path=(AccessPathStep_GetItem_Str(key="repeated_type"),),
            spec=TypeSpec_TypeSpec(),
            value=self.repeated_type,
        )

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        return {
            "$kind": "var_tuple",
            "repeated_type": self.repeated_type.self_to_ipld(),
        }

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_VariableSizeTuple:
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "var_tuple", f"Expected var_tuple kind"

        repeated_type_data = ipld_value["repeated_type"]
        repeated_type = ipld_to_type_spec(repeated_type_data, lookup)

        return cls(repeated_type=repeated_type)


@dataclasses.dataclass(frozen=True)
class TypeSpec_FrozenDictStringKey(TypeSpecBase):
    """A specification for frozen dictionaries with string keys and typed values."""

    value_type: TypeSpecBase

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        return isinstance(other, TypeSpec_FrozenDictStringKey) and self.value_type.is_supertype_of(
            other.value_type
        )

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if not isinstance(value, frozendict):
            raise ValidationError(f"Expected frozendict, got {type(value).__name__}")

        for k, v in value.items():
            if not isinstance(k, str):
                raise ValidationError(f"Expected string key, got {type(k).__name__}")

            try:
                self.value_type.validate(v)
            except ValidationError as e:
                e.add_note(f"In value for key {repr(k)}")
                raise

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse all dictionary values."""
        for k, v in value.items():
            visitor.visit_part_at_path(
                py_path=(AccessPathStep_GetItem_Str(key=k),),
                ipld_path=(AccessPathStep_GetItem_Str(key=k),),
                spec=self.value_type,
                value=v,
            )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        return {k: self.value_type.value_to_ipld(v) for k, v in value.items()}

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        if not isinstance(ipld_value, dict):
            raise ValidationError(
                f"Expected dict for frozendict deserialization, got {type(ipld_value).__name__}"
            )

        result_items = {}
        for k, v_data in ipld_value.items():
            if not isinstance(k, str):
                raise ValidationError(f"Expected string key, got {type(k).__name__}")
            value = self.value_type.ipld_to_value(v_data, lookup)
            result_items[k] = value

        return frozendict(result_items)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse the value type."""
        visitor.visit_part_at_path(
            py_path=(AccessPathStep_GetAttr(attr_name="value_type"),),
            ipld_path=(AccessPathStep_GetItem_Str(key="value_type"),),
            spec=TypeSpec_TypeSpec(),
            value=self.value_type,
        )

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        return {
            "$kind": "fdict_str",
            "value_type": self.value_type.self_to_ipld(),
        }

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_FrozenDictStringKey:
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "fdict_str", f"Expected fdict_str kind"

        value_type_data = ipld_value["value_type"]
        value_type = ipld_to_type_spec(value_type_data, lookup)

        return cls(value_type=value_type)


@dataclasses.dataclass(frozen=True)
class TypeSpec_FrozenDictEntityKey(TypeSpecBase):
    """A specification for frozen dictionaries with entity keys and typed values.

    Entity keys are stored as a map from base32 CID to [key_link, value] tuple. This represents
    the mapping structure faithfully while also allowing us to enumerate the key objects using
    ordinary IPLD operations. (It is slightly redundant in that the CID appears twice, once as a
    key and once as a link.)
    """

    key_type: TypeSpecForSomeKindOfEntity
    value_type: TypeSpecBase

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        return (
            isinstance(other, TypeSpec_FrozenDictEntityKey)
            and self.key_type.is_supertype_of(other.key_type)
            and self.value_type.is_supertype_of(other.value_type)
        )

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if not isinstance(value, frozendict):
            raise ValidationError(f"Expected frozendict, got {type(value).__name__}")

        for k, v in value.items():
            try:
                self.key_type.validate(k)
            except ValidationError as e:
                e.add_note(f"In key {repr(k)}")
                raise

            try:
                self.value_type.validate(v)
            except ValidationError as e:
                e.add_note(f"In value for key {repr(k)}")
                raise

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse all dictionary keys and values."""
        # Sort keys by their CID to get consistent ordering
        sorted_keys = sorted(value.keys(), key=lambda k: k._fxtr_cid.encode("base32"))

        for i, k in enumerate(sorted_keys):
            v = value[k]

            # Visit the key - stored as first element of [key_link, value] pair
            # When accessing in Python, we need to use the sorted key index to retrieve the key.
            key_py_path = (AccessPathStep_FrozenDictSortedKey(sorted_key_index=i),)
            key_ipld_path = (
                AccessPathStep_GetItem_Int(key=i),
                AccessPathStep_GetItem_Int(key=0),
            )
            visitor.visit_part_at_path(
                py_path=key_py_path, ipld_path=key_ipld_path, spec=self.key_type, value=k
            )

            # Visit the value - stored as second element of [key_link, value] pair
            value_py_path = (AccessPathStep_GetItem_Entity(key=k),)
            value_ipld_path = (
                AccessPathStep_GetItem_Int(key=i),
                AccessPathStep_GetItem_Int(key=1),
            )
            visitor.visit_part_at_path(
                py_path=value_py_path, ipld_path=value_ipld_path, spec=self.value_type, value=v
            )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        # For entity keys, we create a sorted list of [key_link, value] pairs
        # Sort by CID to get consistent ordering
        sorted_items = sorted(value.items(), key=lambda item: item[0]._fxtr_cid.encode("base32"))
        result = []
        for k, v in sorted_items:
            key_link = self.key_type.value_to_ipld(k)
            value_ipld = self.value_type.value_to_ipld(v)
            result.append([key_link, value_ipld])
        return result

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        if not isinstance(ipld_value, list):
            raise ValidationError(
                f"Expected list for frozendict deserialization, got {type(ipld_value).__name__}"
            )

        # First pass: deserialize all key-value pairs
        items = []
        for pair_data in ipld_value:
            if not isinstance(pair_data, list) or len(pair_data) != 2:
                raise ValidationError(f"Expected [key_link, value] pair, got {pair_data}")

            key_link_data, value_data = pair_data
            key = self.key_type.ipld_to_value(key_link_data, lookup)
            value = self.value_type.ipld_to_value(value_data, lookup)
            items.append((key, value))

        # Assert that the pairs are sorted by key CID
        original_cids = [key._fxtr_cid.encode("base32") for key, _ in items]
        sorted_cids = sorted(original_cids)
        assert original_cids == sorted_cids, (
            f"IPLD frozendict pairs must be sorted by key CID: got {original_cids}, "
            f"expected {sorted_cids}"
        )

        # Convert to frozendict
        return frozendict(items)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse key and value types."""
        visitor.visit_part_at_path(
            py_path=(AccessPathStep_GetAttr(attr_name="key_type"),),
            ipld_path=(AccessPathStep_GetItem_Str(key="key_type"),),
            spec=TypeSpec_TypeSpec(),
            value=self.key_type,
        )
        visitor.visit_part_at_path(
            py_path=(AccessPathStep_GetAttr(attr_name="value_type"),),
            ipld_path=(AccessPathStep_GetItem_Str(key="value_type"),),
            spec=TypeSpec_TypeSpec(),
            value=self.value_type,
        )

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        return {
            "$kind": "fdict_ent",
            "key_type": self.key_type.self_to_ipld(),
            "value_type": self.value_type.self_to_ipld(),
        }

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_FrozenDictEntityKey:
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "fdict_ent", f"Expected fdict_ent kind"

        key_type_data = ipld_value["key_type"]
        key_type = ipld_to_type_spec(key_type_data, lookup)
        if not isinstance(key_type, TypeSpecForSomeKindOfEntity):
            raise ValidationError(f"Expected entity type spec for key_type, got {type(key_type)}")

        value_type_data = ipld_value["value_type"]
        value_type = ipld_to_type_spec(value_type_data, lookup)

        return cls(key_type=key_type, value_type=value_type)


@dataclasses.dataclass(frozen=True)
class TypeSpec_FrozenDictCIDKey(TypeSpecBase):
    """A specification for frozen dictionaries with CID keys and typed values.

    CID keys are stored as base32-encoded strings in IPLD to ensure compatibility with
    JSON-like object model. During traversal, Python paths use AccessPathStep_GetItem_CID
    while IPLD paths use AccessPathStep_GetItem_Str with the base32-encoded key.
    """

    value_type: TypeSpecBase

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        return isinstance(other, TypeSpec_FrozenDictCIDKey) and self.value_type.is_supertype_of(
            other.value_type
        )

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if not isinstance(value, frozendict):
            raise ValidationError(f"Expected frozendict, got {type(value).__name__}")

        for k, v in value.items():
            if not isinstance(k, cbrrr.CID):
                raise ValidationError(f"Expected CID key, got {type(k).__name__}")

            try:
                self.value_type.validate(v)
            except ValidationError as e:
                e.add_note(f"In value for key {repr(k)}")
                raise

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse all dictionary values."""
        for k, v in value.items():
            # Python path uses CID key directly, IPLD path uses base32-encoded string
            visitor.visit_part_at_path(
                py_path=(AccessPathStep_GetItem_CID(key=k),),
                ipld_path=(AccessPathStep_GetItem_Str(key=k.encode("base32")),),
                spec=self.value_type,
                value=v,
            )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        """Convert to IPLD with CID keys encoded as base32 strings."""
        result = {}
        for k, v in value.items():
            # Encode CID key as base32 string for IPLD compatibility
            base32_key = k.encode("base32")
            result[base32_key] = self.value_type.value_to_ipld(v)
        return result

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        """Reconstruct from IPLD with string keys decoded back to CIDs."""
        if not isinstance(ipld_value, dict):
            raise ValidationError(
                f"Expected dict for frozendict deserialization, got {type(ipld_value).__name__}"
            )

        result_items = {}
        for k, v_data in ipld_value.items():
            if not isinstance(k, str):
                raise ValidationError(f"Expected string key in IPLD, got {type(k).__name__}")

            # Decode base32 string back to CID
            try:
                cid_key = cbrrr.CID.decode(k)
            except Exception as e:
                raise ValidationError(f"Invalid base32 CID key '{k}': {e}")

            value = self.value_type.ipld_to_value(v_data, lookup)
            result_items[cid_key] = value

        return frozendict(result_items)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse the value type."""
        visitor.visit_part_at_path(
            py_path=(AccessPathStep_GetAttr(attr_name="value_type"),),
            ipld_path=(AccessPathStep_GetItem_Str(key="value_type"),),
            spec=TypeSpec_TypeSpec(),
            value=self.value_type,
        )

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert the type spec itself to IPLD."""
        return {
            "$kind": "fdict_cid",
            "value_type": self.value_type.self_to_ipld(),
        }

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_FrozenDictCIDKey:
        """Reconstruct type spec from IPLD."""
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "fdict_cid", f"Expected fdict_cid kind"

        value_type_data = ipld_value["value_type"]
        value_type = ipld_to_type_spec(value_type_data, lookup)

        return cls(value_type=value_type)


@dataclasses.dataclass(frozen=True)
class TypeSpec_FrozenSetOfEntities(TypeSpecBase):
    """A specification for frozen sets with entity items.

    Entity items are stored as a map from base32 CID to entity link. This represents
    the set structure faithfully while also allowing us to enumerate the entity objects using
    ordinary IPLD operations.
    """

    item_type: TypeSpecForSomeKindOfEntity

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        return isinstance(other, TypeSpec_FrozenSetOfEntities) and self.item_type.is_supertype_of(
            other.item_type
        )

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if not isinstance(value, frozenset):
            raise ValidationError(f"Expected frozenset, got {type(value).__name__}")

        for item in value:
            try:
                self.item_type.validate(item)
            except ValidationError as e:
                e.add_note(f"In set item {repr(item)}")
                raise

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse all set items."""
        # Sort items by their CID to get consistent ordering
        sorted_items = sorted(value, key=lambda item: item._fxtr_cid.encode("base32"))

        for i, item in enumerate(sorted_items):
            # Visit the item
            item_py_path = (AccessPathStep_SetElementSortedOrder(sorted_element_index=i),)
            item_ipld_path = (AccessPathStep_GetItem_Int(key=i),)
            visitor.visit_part_at_path(
                py_path=item_py_path, ipld_path=item_ipld_path, spec=self.item_type, value=item
            )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        # For entity sets, we create a sorted list of entity links
        # Sort by CID to get consistent ordering
        sorted_items = sorted(value, key=lambda item: item._fxtr_cid.encode("base32"))
        result = []
        for item in sorted_items:
            item_link = self.item_type.value_to_ipld(item)
            result.append(item_link)
        return result

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        if not isinstance(ipld_value, list):
            raise ValidationError(
                f"Expected list for frozenset deserialization, got {type(ipld_value).__name__}"
            )

        # First pass: deserialize all items
        items = []
        for item_link_data in ipld_value:
            item = self.item_type.ipld_to_value(item_link_data, lookup)
            items.append(item)

        # Assert that the items are sorted by CID
        original_cids = [item._fxtr_cid.encode("base32") for item in items]
        sorted_cids = sorted(original_cids)
        assert original_cids == sorted_cids, (
            f"IPLD frozenset items must be sorted by CID: got {original_cids}, "
            f"expected {sorted_cids}"
        )

        # Convert to frozenset
        return frozenset(items)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse item type."""
        visitor.visit_part_at_path(
            py_path=(AccessPathStep_GetAttr(attr_name="item_type"),),
            ipld_path=(AccessPathStep_GetItem_Str(key="item_type"),),
            spec=TypeSpec_TypeSpec(),
            value=self.item_type,
        )

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        return {
            "$kind": "fset_ent",
            "item_type": self.item_type.self_to_ipld(),
        }

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_FrozenSetOfEntities:
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "fset_ent", f"Expected fset_ent kind"

        item_type_data = ipld_value["item_type"]
        item_type = ipld_to_type_spec(item_type_data, lookup)
        if not isinstance(item_type, TypeSpecForSomeKindOfEntity):
            raise ValidationError("Expected entity type spec for item_type, got {type(item_type)}")

        return cls(item_type=item_type)


# endregion
####################################################################################################
# region: Variant/union type specifications
####################################################################################################


@dataclasses.dataclass(frozen=True)
class TypeSpec_Optional(TypeSpecBase):
    """A specification for optional values (Type | None)."""

    inner_type: TypeSpecBase

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        if isinstance(other, TypeSpec_Optional):
            return self.inner_type.is_supertype_of(other.inner_type)
        elif isinstance(other, TypeSpec_Primitive) and other.primitive_type is type(None):
            # Optional[T] includes None
            return True
        else:
            # Optional[T] includes T
            return self.inner_type.is_supertype_of(other)

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if value is None:
            return

        try:
            self.inner_type.validate(value)
        except ValidationError as e:
            e.add_note("In optional value")
            raise

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse the optional value."""
        if value is not None:
            self.inner_type.traverse_value(value, visitor)

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        """Convert optional value to IPLD."""
        if value is None:
            return None
        return self.inner_type.value_to_ipld(value)

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        """Reconstruct optional value from IPLD."""
        if ipld_value is None:
            return None
        return self.inner_type.ipld_to_value(ipld_value, lookup)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse the inner type."""
        visitor.visit_part_at_path(
            py_path=(AccessPathStep_GetAttr(attr_name="inner_type"),),
            ipld_path=(AccessPathStep_GetItem_Str(key="inner_type"),),
            spec=TypeSpec_TypeSpec(),
            value=self.inner_type,
        )

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert optional type to IPLD."""
        return {
            "$kind": "optional",
            "inner_type": self.inner_type.self_to_ipld(),
        }

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_Optional:
        """Reconstruct optional type from IPLD."""
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "optional", f"Expected optional kind"

        inner_type_data = ipld_value["inner_type"]
        inner_type = ipld_to_type_spec(inner_type_data, lookup)

        return cls(inner_type=inner_type)


@dataclasses.dataclass(frozen=True)
class TypeSpec_UnionOfEntities(TypeSpecBase):
    """The type of a union of known entity types."""

    entity_types: frozenset[TypeSpecBase_SomeEntity]

    def _get_sorted_types(self) -> list[tuple[bytes, cbrrr.DagCborTypes, TypeSpecBase_SomeEntity]]:
        """Helper method to get entity types sorted by DAG-CBOR codec bytes."""
        type_tuples = []
        for entity_type in self.entity_types:
            type_ipld = entity_type.self_to_ipld()
            type_bytes = cbor_util.encode_cbor(type_ipld)
            type_tuples.append((type_bytes, type_ipld, entity_type))

        # Sort by the DAG-CBOR bytes
        return sorted(type_tuples, key=lambda x: x[0])

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        if isinstance(other, TypeSpec_UnionOfEntities):
            # This union is a supertype if it contains all entity types from the other union
            return other.entity_types.issubset(self.entity_types)
        elif isinstance(other, TypeSpecBase_SomeEntity):
            # This union is a supertype if it contains the specific entity type
            return other in self.entity_types
        else:
            return False

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        # Check if the value has a _fxtr_typespec attribute indicating it's an entity
        if not hasattr(value, "_fxtr_typespec"):
            raise ValidationError(
                f"Expected an entity (with _fxtr_typespec attribute), got {type(value).__name__}"
            )

        entity_type_spec = value._fxtr_typespec
        if not isinstance(entity_type_spec, TypeSpecBase_SomeEntity):
            raise ValidationError(
                "Expected entity with TypeSpecBase_SomeEntity type spec, got"
                f" {type(entity_type_spec)}"
            )

        # Check if this entity's type is in our union
        if entity_type_spec not in self.entity_types:
            raise ValidationError(
                f"Entity type {entity_type_spec} is not in union of {len(self.entity_types)} types"
            )

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse the union value."""
        # Get the entity's actual type spec directly
        if not hasattr(value, "_fxtr_typespec"):
            raise ValidationError(
                f"Expected an entity (with _fxtr_typespec attribute), got {type(value).__name__}"
            )

        entity_type_spec = value._fxtr_typespec

        # Traverse using the entity's actual type spec
        visitor.visit_part_at_path(
            py_path=(),
            ipld_path=(),
            spec=entity_type_spec,
            value=value,
        )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        """Convert union value to IPLD."""
        # Get the entity's actual type spec directly
        if not hasattr(value, "_fxtr_typespec"):
            raise ValidationError(
                f"Expected an entity (with _fxtr_typespec attribute), got {type(value).__name__}"
            )

        entity_type_spec = value._fxtr_typespec

        # Serialize using the entity's actual type spec
        return entity_type_spec.value_to_ipld(value)

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        """Reconstruct union value from IPLD."""
        if not isinstance(ipld_value, cbrrr.CID):
            raise ValidationError(f"Expected CID for union deserialization, got {type(ipld_value)}")

        # Load the entity from the CID
        entity = lookup(ipld_value)

        # Validate that the loaded entity matches one of our union types
        try:
            self.validate(entity)
        except ValidationError as e:
            e.add_note(f"While deserializing entity from CID {ipld_value}")
            raise

        return entity

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse all entity types in the union."""
        sorted_types = self._get_sorted_types()

        for i, (_, _, entity_type) in enumerate(sorted_types):
            visitor.visit_part_at_path(
                py_path=(
                    AccessPathStep_GetAttr(attr_name="entity_types"),
                    AccessPathStep_SetElementSortedOrder(sorted_element_index=i),
                ),
                ipld_path=(
                    AccessPathStep_GetItem_Str(key="entity_types"),
                    AccessPathStep_GetItem_Int(key=i),
                ),
                spec=TypeSpec_TypeSpec(),
                value=entity_type,
            )

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert union type to IPLD."""
        sorted_types = self._get_sorted_types()
        entity_types_list = [type_ipld for _, type_ipld, _ in sorted_types]

        return {"$kind": "ent_union", "entity_types": entity_types_list}

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_UnionOfEntities:
        """Reconstruct union type from IPLD."""
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "ent_union", f"Expected ent_union kind"

        entity_types_data = ipld_value["entity_types"]
        assert isinstance(entity_types_data, list), f"Expected list for entity_types"

        entity_types = set()
        for entity_type_data in entity_types_data:
            entity_type = ipld_to_type_spec(entity_type_data, lookup)
            if not isinstance(entity_type, TypeSpecBase_SomeEntity):
                raise ValidationError(f"Expected TypeSpecBase_SomeEntity, got {type(entity_type)}")
            entity_types.add(entity_type)

        return cls(entity_types=frozenset(entity_types))


@dataclasses.dataclass(frozen=True)
class TypeSpec_AnyEntity(TypeSpecBase):
    """The type for an entity of any type - acts like a union over all possible entity types."""

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        # AnyEntity is a supertype of any entity type or union of entity types, including service identifiers
        return isinstance(other, TypeSpecForSomeKindOfEntity)

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        # Check if the value has a _fxtr_typespec attribute indicating it's an entity
        if not hasattr(value, "_fxtr_typespec"):
            raise ValidationError(
                f"Expected an entity (with _fxtr_typespec attribute), got {type(value).__name__}"
            )

        entity_type_spec = value._fxtr_typespec
        if not isinstance(entity_type_spec, TypeSpecBase_SomeEntity):
            raise ValidationError(
                "Expected entity with TypeSpecBase_SomeEntity type spec, got"
                f" {type(entity_type_spec)}"
            )

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse the any-entity value."""
        # Get the entity's actual type spec directly
        if not hasattr(value, "_fxtr_typespec"):
            raise ValidationError(
                f"Expected an entity (with _fxtr_typespec attribute), got {type(value).__name__}"
            )

        entity_type_spec = value._fxtr_typespec

        # Traverse using the entity's actual type spec
        visitor.visit_part_at_path(
            py_path=(),
            ipld_path=(),
            spec=entity_type_spec,
            value=value,
        )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        """Convert any-entity value to IPLD."""
        # Get the entity's actual type spec directly
        if not hasattr(value, "_fxtr_typespec"):
            raise ValidationError(
                f"Expected an entity (with _fxtr_typespec attribute), got {type(value).__name__}"
            )

        entity_type_spec = value._fxtr_typespec

        # Serialize using the entity's actual type spec
        return entity_type_spec.value_to_ipld(value)

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        """Reconstruct any-entity value from IPLD."""
        if not isinstance(ipld_value, cbrrr.CID):
            raise ValidationError(
                f"Expected CID for any-entity deserialization, got {type(ipld_value)}"
            )

        # Load the entity from the CID - any entity type is valid
        return lookup(ipld_value)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """AnyEntity type contains no specific entity references."""

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert any-entity type to IPLD."""
        return {"$kind": "any_entity"}

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_AnyEntity:
        """Reconstruct any-entity type from IPLD."""
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "any_entity", f"Expected any_entity kind"
        return cls()


@dataclasses.dataclass(frozen=True)
class TypeSpec_AnyExternalServiceIdentifier(TypeSpecBase):
    """The type for a service identifier of any type - acts like a union over all possible service identifier types."""

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        # AnyExternalServiceIdentifier is a supertype of any service identifier type
        return isinstance(
            other, (ExternalServiceIdentifier.typespec_cls, TypeSpec_AnyExternalServiceIdentifier)
        )

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        # Check if the value is a service identifier
        if not isinstance(value, ExternalServiceIdentifier):
            raise ValidationError(f"Expected service identifier, got {type(value).__name__}")

        # Check that it has a proper service identifier typespec
        entity_type_spec = value._fxtr_typespec
        if not isinstance(entity_type_spec, ExternalServiceIdentifier.typespec_cls):
            raise ValidationError(
                "Expected entity with ExternalServiceIdentifier.typespec_cls type spec, got"
                f" {type(entity_type_spec)}"
            )

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse the any-service-identifier value."""
        # Get the service identifier's actual type spec directly
        if not isinstance(value, ExternalServiceIdentifier):
            raise ValidationError(f"Expected service identifier, got {type(value).__name__}")

        entity_type_spec = value._fxtr_typespec

        # Traverse using the service identifier's actual type spec
        visitor.visit_part_at_path(
            py_path=(),
            ipld_path=(),
            spec=entity_type_spec,
            value=value,
        )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        """Convert any-service-identifier value to IPLD."""
        # Get the service identifier's actual type spec directly
        if not isinstance(value, ExternalServiceIdentifier):
            raise ValidationError(f"Expected service identifier, got {type(value).__name__}")

        entity_type_spec = value._fxtr_typespec

        # Serialize using the service identifier's actual type spec
        return entity_type_spec.value_to_ipld(value)

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        """Reconstruct any-service-identifier value from IPLD."""
        if not isinstance(ipld_value, cbrrr.CID):
            raise ValidationError(
                f"Expected CID for any-service-identifier deserialization, got {type(ipld_value)}"
            )

        # Load the entity from the CID
        entity = lookup(ipld_value)

        # Validate that the loaded entity is a service identifier
        try:
            self.validate(entity)
        except ValidationError as e:
            e.add_note(f"While deserializing service identifier from CID {ipld_value}")
            raise

        return entity

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """AnyExternalServiceIdentifier type contains no specific service identifier references."""

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert any-service-identifier type to IPLD."""
        return {"$kind": "any_ext_svc_id"}

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_AnyExternalServiceIdentifier:
        """Reconstruct any-service-identifier type from IPLD."""
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "any_ext_svc_id", f"Expected any_ext_svc_id kind"
        return cls()


# endregion
####################################################################################################
# region: Builtin type helpers
####################################################################################################


class TypeSpecBase_SomeBuiltinEntity(TypeSpecBase_SomeEntity):
    """A base class for unparameterized type specs that represent some builtin entity type."""

    entity_cls: ClassVar[type[BuiltinEntityBase]]
    kind: ClassVar[str]

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        return type(other) == type(self)

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if not isinstance(value, self.entity_cls):
            raise ValidationError(f"Expected {self.entity_cls}, got {type(value).__name__}")

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse the builtin entity."""
        value.traverse_self(visitor)

    def entity_data_to_ipld(self, entity: BuiltinEntityBase) -> cbrrr.DagCborTypes:  # type: ignore
        """Convert BuiltinEntityBase data to IPLD (serializes hardcoded attributes, NOT doc)."""
        return entity.contents_to_ipld()

    def ipld_to_entity_object(
        self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> BuiltinEntityBase:
        """Reconstruct the entity from IPLD representation."""
        return self.entity_cls.ipld_to_this_type(ipld_value, lookup)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """TypeSpecBase_SomeBuiltinEntity has no sub-structure to traverse."""

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert the type spec itself to IPLD."""
        return {"$kind": self.kind}

    @classmethod
    def ipld_to_this_type(cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Self:
        """Reconstruct type spec from IPLD."""
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == cls.kind, f"Expected {cls.kind} kind"
        return cls()


class BuiltinEntityBase(EntityBase):
    """A base class for builtin entities."""

    _fxtr_cid: cbrrr.CID
    typespec_cls: ClassVar[type[TypeSpecBase_SomeBuiltinEntity]]

    def __init__(self) -> None:
        # Compute and cache the CID of the entity immediately.
        object.__setattr__(self, "_fxtr_cid", _compute_entity_cid(self))

        # Notify the active entity creation hook that this entity has been created.
        _notify_entity_created(self)

        super().__init__()

    def __init_subclass__(cls, *, typespec_kind: str | None, **kwargs) -> None:
        if hasattr(cls, "typespec_cls"):
            raise ValueError(
                f"Cannot create {cls.__name__} as a subclass of BuiltinEntityBase: "
                f"it already has a typespec_cls {cls.typespec_cls}"
            )

        if typespec_kind is not None:

            class TypeSpec_ThisBuiltinEntity(TypeSpecBase_SomeBuiltinEntity):
                entity_cls = cls
                kind = typespec_kind

            TypeSpec_ThisBuiltinEntity.__name__ = f"TypeSpec_{cls.__name__}"
            cls.typespec_cls = TypeSpec_ThisBuiltinEntity
        super().__init_subclass__(**kwargs)

    @property
    def _fxtr_typespec(self) -> TypeSpecBase_SomeBuiltinEntity:  # type: ignore
        """Get the typespec for the entity."""
        return self.typespec_cls()

    @abc.abstractmethod
    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse the entity."""

    @abc.abstractmethod
    def contents_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert the entity contents to IPLD."""

    @classmethod
    @abc.abstractmethod
    def ipld_to_this_type(cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Self:
        """Reconstruct the entity from the IPLD representation of its contents."""

    def as_ipld_link(self) -> cbrrr.DagCborTypes:
        """Convenience method to convert the entity to an IPLD link."""
        return self.typespec_cls().value_to_ipld(self)


class BuiltinEntityFromAnnotations(BuiltinEntityBase, typespec_kind=None):
    """A base class for builtin entities that define their attributes via annotations.

    Designed for use with subclasses that are dataclasses.
    """

    def __post_init__(self):
        # Default __post_init__ to simplify subclasses that are dataclasses.

        # First validate each field against its expected type.
        field_typespecs = self._field_typespecs()
        for field_name, field_typespec in field_typespecs.items():
            field_value = getattr(self, field_name)
            field_typespec.validate(field_value)

        # Then call superclass init logic, which will compute the CID and notify the creation hook.
        super().__init__()

    @classmethod
    def _field_typespecs(cls) -> dict[str, TypeSpecBase]:
        """Get the typespecs for the fields of the entity."""
        result = {}
        annotations = inspect.get_annotations(cls, eval_str=True)
        for field_name, field_type in annotations.items():
            if field_name == "_fxtr_cid":
                continue
            result[field_name] = python_type_to_hub_type_spec(field_type)
        return result

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse the dataclass entity by visiting its field values."""
        field_typespecs = self._field_typespecs()
        for field_name, field_typespec in field_typespecs.items():
            field_value = getattr(self, field_name)
            visitor.visit_part_at_path(
                py_path=(AccessPathStep_GetAttr(attr_name=field_name),),
                ipld_path=(AccessPathStep_GetItem_Str(key=field_name),),
                spec=field_typespec,
                value=field_value,
            )

    def contents_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert the dataclass entity to IPLD by serializing its fields."""
        result = {}
        result["$typespec"] = self._fxtr_typespec.self_to_ipld()

        field_typespecs = self._field_typespecs()
        for field_name, field_typespec in field_typespecs.items():
            field_value = getattr(self, field_name)
            result[field_name] = field_typespec.value_to_ipld(field_value)

        return result

    @classmethod
    def ipld_to_this_type(cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Self:
        """Reconstruct the dataclass entity from IPLD representation."""
        if not isinstance(ipld_value, dict):
            raise ValidationError(
                f"Expected dict for {cls.__name__}, got {type(ipld_value).__name__}"
            )

        # Note: $typespec key is already validated by the caller

        # Get field typespecs from the class
        field_typespecs = cls._field_typespecs()

        # Reconstruct each field from IPLD using its typespec
        field_data = {}
        for field_name, field_typespec in field_typespecs.items():
            if field_name not in ipld_value:
                raise ValidationError(f"Missing required field: {field_name}")
            field_ipld = ipld_value[field_name]
            field_data[field_name] = field_typespec.ipld_to_value(field_ipld, lookup)

        # Create the instance using the reconstructed field data
        return cls(**field_data)


# endregion
####################################################################################################
# region: User-defined entity types and associated helpers
####################################################################################################


class UserDefinedEntityMeta(abc.ABCMeta):
    """Metaclass for custom entity types that enables proper type equality and virtual subclassing.

    Two classes with the same value_typespec are considered equal and virtual subclasses of each
    other. This enables isinstance/issubclass checks to work properly across dynamically generated
    entity classes. To make this slightly safer, it is not allowed to subclass any class with this
    metaclass that already has a value_typespec. (This is not fully robust, because we do not check
    that other things like method implementations and base classes match.)

    Used for both data entities and service-identifier entities.

    ABCMeta-style registration of virtual subclasses is not supported.
    """

    value_typespec: TypeSpecBase_SomeEntity

    def __new__(cls, name, bases, namespace, **kwargs):
        """Create a new user-defined entity class."""
        for base in bases:
            if hasattr(base, "value_typespec"):
                raise ValueError(
                    f"Cannot create {name} as a subclass of UserDefinedEntityMeta: it has a base"
                    f" class {base.__name__} with an existing value_typespec {base.value_typespec}"
                )

        return super().__new__(cls, name, bases, namespace, **kwargs)

    def __eq__(cls, other):
        """Two entity classes are equal if they have the same value_typespec."""
        if not isinstance(other, UserDefinedEntityMeta):
            return False
        if hasattr(cls, "value_typespec") and hasattr(other, "value_typespec"):
            return cls.value_typespec == other.value_typespec
        return super().__eq__(other)

    def __hash__(cls):
        """Hash based on the typespec for consistent behavior."""
        if hasattr(cls, "value_typespec"):
            return hash(cls.value_typespec)
        return super().__hash__()

    def __subclasscheck__(cls, subclass):
        """Check if subclass is a virtual subclass based on value_typespec equality."""
        if (
            isinstance(subclass, UserDefinedEntityMeta)
            and hasattr(subclass, "value_typespec")
            and hasattr(cls, "value_typespec")
        ):
            return cls.value_typespec == subclass.value_typespec
        # Bypass ABCMeta's own virtual subclass checks (not allowed for UserDefinedEntityMeta)
        return type.__subclasscheck__(cls, subclass)

    def __instancecheck__(cls, instance):
        """Check if instance is of this type based on value_typespec equality."""
        instance_ty = type(instance)
        if (
            isinstance(instance_ty, UserDefinedEntityMeta)
            and hasattr(instance_ty, "value_typespec")
            and hasattr(cls, "value_typespec")
        ):
            return cls.value_typespec == instance_ty.value_typespec
        # Bypass ABCMeta's own virtual subclass checks (not allowed for UserDefinedEntityMeta)
        return type.__instancecheck__(cls, instance)


def _get_type_namespace_id(arg: str | None) -> str:
    """Get a current namespace ID, or the default if not provided.

    Raises ValueError if no namespace ID is provided and no default is set.
    """
    if arg is not None:
        return arg

    entity_creation_st = _ENTITY_CREATION_STATE.get()
    if entity_creation_st is not None and entity_creation_st.default_namespace_id is not None:
        return entity_creation_st.default_namespace_id

    raise ValueError(
        "No default namespace ID is set. This likely means a fxtr type is being defined outside"
        " of a namespaced collection. Please provide an explicit namespace ID or open a"
        " collection."
    )


@contextlib.contextmanager
def _using_type_namespace_id(namespace_id: str | None) -> Generator[None, None, None]:
    """Context manager to temporarily set the type namespace ID."""
    entity_creation_st = _ENTITY_CREATION_STATE.get()
    if entity_creation_st is None:
        new_entity_creation_st = EntityCreationState(
            creation_hook=None,
            default_namespace_id=namespace_id,
        )
    else:
        new_entity_creation_st = EntityCreationState(
            creation_hook=entity_creation_st.creation_hook,
            default_namespace_id=namespace_id,
        )
    with _ENTITY_CREATION_STATE.locally_scoped_assign(new_entity_creation_st):
        yield


# endregion
####################################################################################################
# region: Data entities and their type specifications
####################################################################################################

IGNORED_CLASS_DICT_KEYS = frozenset(
    {
        "__annotations__",
        "__class_getitem__",
        "__dict__",
        "__doc__",
        "__firstlineno__",
        "__module__",
        "__name__",
        "__orig_bases__",
        "__parameters__",
        "__qualname__",
        "__static_attributes__",
        "__subclasshook__",
        "__type_params__",
        "__weakref__",
    }
)


@typing.dataclass_transform(frozen_default=True)
class DataEntityBase(EntityBase, metaclass=UserDefinedEntityMeta):
    """Base class for Python representations of data entities, as Python dataclasses.

    For convenience on the Python side, data entity schemas are represented as distinct Python
    classes, which are each dataclasses, and which are constructed dynamically. However, two data
    entity classes with the same schema are considered equal as types, and instances of one are
    instances of another.

    DataEntityBase classes are constructed in a somewhat circular fashion, to enable easier use
    from Python while also making it possible to deserialize schema types from IPLD. Each subclass
    of DataEntityBase has a `value_typespec` class attribute that is a TypeSpec_DataEntity object,
    which then points back at the DataEntityBase subclass with a weak reference. When a
    DataEntityBase subclass is constructed _without_ a _fxtr_value_typespec metaclass argument,
    it infers its schema from its type annotations, and constructs a typespec for itself. When a
    DataEntityBase subclass is constructed _with_ a _fxtr_value_typespec metaclass argument, it
    overrides its own type annotations with those from the provided typespec.
    """

    value_typespec: ClassVar[TypeSpec_DataEntity]

    def __init_subclass__(
        cls,
        *,
        type_id: str | None = None,
        namespace_id: str | None = None,
        schema_forward_defns: dict[str, EntityForwardRefMetadata] | None = None,
        _value_typespec: TypeSpec_DataEntity | None = None,
        **kwargs,
    ):
        """Create a new data entity type.

        Either (type_id, namespace_id) or value_typespec must be provided.

        If value_typespec is provided, it overrides the class's type annotations with those
        from the provided typespec. Otherwise, it infers its schema from its type annotations, and
        constructs a typespec for itself.

        If schema_forward_defns is provided, it should be a map from type names to
        EntityForwardRefMetadata instances. These forward references will be substituted in place of
        references to those types in the type annotations. You do not need to provide a forward
        reference for the type itself; this will always be done automatically.
        Note that if you provide a forward definition for a type that is actually already in scope,
        we will still use the forward definition in the schema instead of referencing the actual
        type definition, to prevent accidental changes to the schema.

        The class is then transformed into a dataclass, and the value_typespec class attribute is
        set to the constructed typespec.
        """
        if _value_typespec is not None:
            if type_id is not None or namespace_id is not None:
                raise ValueError("Cannot provide both value_typespec and type_id/namespace_id")
        elif type_id is None:
            raise ValueError(
                "Must provide type_id and namespace_id when constructing a data entity subclass"
                " from Python!"
            )
        else:
            namespace_id = _get_type_namespace_id(namespace_id)
            assert namespace_id is not None

        # Check that class only contains annotations and optionally a docstring
        for attr_name in cls.__dict__:
            if attr_name not in IGNORED_CLASS_DICT_KEYS:
                raise ValueError(
                    f"Entity class {cls.__name__} contains '{attr_name}' - only type"
                    " annotations and docstring allowed"
                )

        if _value_typespec is None:
            assert type_id is not None and namespace_id is not None

            extended_fwd_defns = {
                cls.__name__: entity_type_fwdref(type_id=type_id, namespace_id=namespace_id)
            }
            if schema_forward_defns is not None:
                assert cls.__name__ not in schema_forward_defns
                extended_fwd_defns.update(schema_forward_defns)

            orig_globals = sys.modules[cls.__module__].__dict__
            extended_globals = dict(orig_globals)
            fwdref_overrides = set()
            for type_name, forward_defn in extended_fwd_defns.items():
                if type_name in orig_globals:
                    referenced_ty = orig_globals[type_name]
                    if not issubclass(referenced_ty, DataEntityBase):
                        raise ValueError(
                            f"Forward reference conflict: Type {type_name} already exists in"
                            " globals and is not a data entity."
                        )
                    tyspec = referenced_ty.value_typespec
                    assert isinstance(tyspec, TypeSpec_DataEntity)
                    if (
                        tyspec.schema.namespace_id != forward_defn.namespace_id
                        or tyspec.schema.type_id != forward_defn.type_id
                    ):
                        raise ValueError(
                            f"Forward reference conflict: Type {type_name} already exists in"
                            " globals and has different namespace or type id."
                        )
                    fwdref_overrides.add(
                        TypeSpec_DataEntityForwardRef(
                            namespace_id=forward_defn.namespace_id,
                            type_id=forward_defn.type_id,
                        )
                    )
                else:
                    extended_globals[type_name] = forward_defn

            # Infer the typespec from the class's type annotations
            try:
                annotations = inspect.get_annotations(cls, globals=extended_globals, eval_str=True)
            except Exception as e:
                e.add_note(f"While evaluating type annotations for entity class {cls.__name__}")
                raise

            with _with_local_data_entity_fwdref_overrides(fwdref_overrides):
                value_typespec = TypeSpec_DataEntity.build(
                    type_id=type_id,
                    namespace_id=namespace_id,
                    field_types=annotations,
                    doc=cls.__doc__,
                )
            logger.debug(f"Inferred value typespec {value_typespec} for {cls.__name__}")

        else:
            value_typespec = _value_typespec
            # Overwrite the class's type annotations with those from the provided typespec
            if cls.__dict__.get("__annotations__") or cls.__dict__.get("__doc__"):
                raise ValueError(
                    "Cannot provide both _value_typespec and type annotations/docstring for entity"
                    f" class {cls.__name__}"
                )
            annotations = {
                name: hub_type_spec_to_python_type(spec)
                for name, spec in value_typespec.schema.field_type_specs.items()
            }
            cls.__annotations__ = annotations
            cls.__doc__ = value_typespec.schema._doc
            logger.debug(f"Dynamically constructing {cls.__name__} from provided typespec")

        # Rebind the name of the class based on the type id.
        cls.__name__ = value_typespec.schema.type_id.split("^", 1)[0]

        # Save the value_typespec class attribute.
        cls.value_typespec = value_typespec

        # Allow base classes to process the subclass.
        super().__init_subclass__(**kwargs)

        # Transform the class into a dataclass. (__eq__ is inherited from EntityBase.)
        dataclasses.dataclass(cls, frozen=True, eq=False)

        # Save this class on the typespec.
        assert value_typespec._py_class_weakref is None
        value_typespec._set_py_class(cls)

    @property
    def _fxtr_typespec(self) -> TypeSpec_DataEntity:  # type: ignore
        """Get the typespec from the class."""
        typespec = type(self).value_typespec
        assert isinstance(typespec, TypeSpec_DataEntity)
        return typespec

    def __post_init__(self):
        # Validate each field against its expected type
        for field_name, field_type_spec in self._fxtr_typespec.schema.field_type_specs.items():
            field_value = getattr(self, field_name)
            try:
                field_type_spec.validate(field_value)
            except ValidationError as e:
                e.add_note(
                    f"In field '{field_name}' of data entity"
                    f" {self._fxtr_typespec.schema.namespace_id}:{self._fxtr_typespec.schema.type_id}"
                )
                raise

        # Compute and cache the CID of the entity immediately.
        object.__setattr__(self, "_fxtr_cid", _compute_entity_cid(self))

        # Notify the active entity creation hook that this entity has been created.
        _notify_entity_created(self)

    def __reduce__(self):
        """Converts the entity to a pickable representation.

        Data entities pickle themselves based on their IPLD representation, making it possible to
        deserialize them without their type being importable from the original location where it
        was defined.
        """
        from fxtr.object_model.entity_util import EntityChildCollector

        visitor = EntityChildCollector()
        self._fxtr_typespec.traverse_value(self, visitor)
        self._fxtr_typespec.traverse_self(visitor)
        ipld_form = self._fxtr_typespec.entity_data_to_ipld(self)
        return (_unpickle_entity, (ipld_form, [node.entity for node in visitor.visited_entities]))


def _unpickle_entity(ipld_form: cbrrr.DagCborTypes, children: list[AnyEntity]) -> AnyEntity:
    cid_to_entity = {entity_cid(entity): entity for entity in children}
    return ipld_entity_data_to_entity(ipld_form, lambda cid: cid_to_entity[cid])


@dataclasses.dataclass(frozen=True, eq=False)
class DataEntitySchema(BuiltinEntityFromAnnotations, typespec_kind="data_ent_schema"):
    """Python representation of an entity defining the type/schema of a data entity.

    Data entity schema entities are used to define the schema of a data entity. They are the
    representation of the schema as an entity in the hub object model. Note that each actual data
    entity is also an instance of a Python type representing this schema; that type is constructed
    dynamically, and refers to the DataEntitySchema entity that defines it.

    DataEntitySchema objects themselves have a built-in singleton typespec, implicitly defined by
    BuiltinEntityBase.

    Attributes:
        namespace_id: The namespace identifier for the entity type.
        type_id: The type identifier for the entity type.
        field_type_specs: A frozendict mapping field names to their types.
        _doc: A documentation string for the entity schema. NOT considered part of the entity, and
            will not be serialized into IPLD or hashed to produce the entity's CID; this means that
            two schemas with the same namespace, type_id, and field types will be considered equal
            even if they have different documentation strings.
    """

    namespace_id: str
    type_id: str
    field_type_specs: frozendict[str, TypeSpecBase]
    _doc: str | None = dataclasses.field(compare=False, repr=False, default=None)

    @classmethod
    def _field_typespecs(cls) -> dict[str, TypeSpecBase]:
        """Get the typespecs for the fields of this schema entity itself."""
        my_specs = super()._field_typespecs()
        del my_specs["_doc"]
        return my_specs


def define_entity_type(
    type_id: str,
    *,
    field_types: dict[str, type] | frozendict[str, type],
    doc: str | None = None,
    namespace_id: str | None = None,
) -> type[Any]:
    """Define a new data entity type with specified field types.

    Args:
        type_id: The type identifier for the entity type.
        field_types: A frozendict mapping field names to their Python types.
        doc: A documentation string for the entity type.
        namespace_id: The namespace identifier for the entity type. If not provided, the default
            namespace ID is used (when available).

    Returns:
        A Python class that can be used to construct data entities.

    Raises:
        ValueError: If type_id doesn't contain '@' separator or field_types are invalid
        TypeAnnotationConversionError: If field types cannot be converted to hub type specs

    Example usage:
        Person = define_entity_type(
            # uuid should generally correspond to a hub package you own
            namespace_id="hub:3a30020b-ac45-47db-abc8-ff6fe4e70f78",
            type_id="Person^1",
            field_types={"name": str, "age": int, "email": str},
            doc="A person entity with basic information"
        )

        # Create instances
        john = Person(name="John Doe", age=30, email="john@example.com")
    """
    namespace_id = _get_type_namespace_id(namespace_id)
    typespec = TypeSpec_DataEntity.build(
        type_id=type_id, namespace_id=namespace_id, field_types=field_types, doc=doc
    )
    return typespec._get_py_class()


# Global cache mapping DataEntitySchema CID -> Python class for data entities.
# Using WeakValueDictionary so classes can be garbage collected when no longer referenced.
# This ensures that all TypeSpec_DataEntity instances with the same schema share
# the same Python class, avoiding repeated class creation and ABC subclass check overhead.
_DATA_ENTITY_PY_CLASS_CACHE: weakref.WeakValueDictionary[cbrrr.CID, type["DataEntityBase"]] = (
    weakref.WeakValueDictionary()
)


@dataclasses.dataclass(frozen=True)
class TypeSpec_DataEntity(TypeSpecBase_SomeEntity):
    """The type spec for user-defined data entities."""

    schema: DataEntitySchema
    _py_class_weakref: weakref.ReferenceType[type[DataEntityBase]] | None = dataclasses.field(
        default=None, init=False, repr=False, compare=False
    )

    @classmethod
    def build(
        cls,
        type_id: str,
        namespace_id: str,
        field_types: dict[str, type] | frozendict[str, type],
        doc: str | None,
    ) -> Self:
        """Validate and construct a new data entity typespec from the given parameters."""
        if "^" not in type_id:
            raise ValueError(
                f"Type ID {repr(type_id)} must contain a version separator '^', e.g."
                " 'MyEntityName^1'. The part before the first '^' will be used as the display name"
                " of the entity; the second component is used to disambiguate different versions"
                " of the same entity type."
            )
        if not isinstance(field_types, (dict, frozendict)):
            raise ValueError(
                f"field_types for a call to define_entity_type(...) must be a dict or frozendict."
            )
        if doc is not None and not isinstance(doc, str):
            raise ValueError(
                f"doc for a call to define_entity_type(...) must be a string, or None."
            )

        for field_name in field_types.keys():
            if field_name.startswith("__"):
                raise ValueError(f"Field name {field_name!r} cannot start with __")
            if field_name == "_fxtr":
                raise ValueError(f"Field names starting with _fxtr are reserved for internal use.")

        # Convert Python type annotations to hub type specs
        hub_field_type_specs = {}
        for field_name, python_type in field_types.items():
            try:
                hub_field_type_specs[field_name] = python_type_to_hub_type_spec(python_type)
            except Exception as e:
                e.add_note(
                    f"While parsing type of field {repr(field_name)} of entity type {type_id}"
                )
                raise

        # Create the schema entity
        schema = DataEntitySchema(
            namespace_id=namespace_id,
            type_id=type_id,
            field_type_specs=frozendict(hub_field_type_specs),
            _doc=doc,
        )
        return cls(schema=schema)

    def _set_py_class(self, py_class: type[DataEntityBase]) -> None:
        """Set the Python class for this data entity type."""
        assert self._py_class_weakref is None
        object.__setattr__(self, "_py_class_weakref", weakref.ref(py_class))

    def _get_py_class(self) -> type[DataEntityBase]:
        """Get or create the Python class for this data entity type."""
        # Check if we have a local cached class reference
        if self._py_class_weakref is not None:
            py_class = self._py_class_weakref()
            if py_class is not None:
                return py_class
            object.__setattr__(self, "_py_class_weakref", None)

        # Check the global cache (shared across all TypeSpec_DataEntity instances
        # with the same schema)
        schema_cid = self.schema._fxtr_cid
        cached_class = _DATA_ENTITY_PY_CLASS_CACHE.get(schema_cid)
        if cached_class is not None:
            object.__setattr__(self, "_py_class_weakref", weakref.ref(cached_class))
            return cached_class

        # Create a new class.
        py_class = types.new_class(
            self.schema.type_id.split("^", 1)[0],
            (DataEntityBase,),
            {"_value_typespec": self},
        )

        # Make sure this new class registered itself with this typespec.
        assert self._py_class_weakref is not None and self._py_class_weakref() is py_class

        # Add to global cache
        _DATA_ENTITY_PY_CLASS_CACHE[schema_cid] = py_class
        return py_class

    def __getstate__(self) -> dict[str, Any]:
        """Return state for pickling, excluding the weakref cache."""
        return {"schema": self.schema}

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore state from pickling."""
        object.__setattr__(self, "schema", state["schema"])
        object.__setattr__(self, "_py_class_weakref", None)

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        return isinstance(other, TypeSpec_DataEntity) and self.schema == other.schema

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        py_cls = self._get_py_class()
        if not isinstance(value, py_cls):
            if isinstance(value, DataEntityBase) and py_cls.__name__ == type(value).__name__:
                # Same name; must be a typespec mismatch?
                raise ValidationError(
                    f"Schema mismatch for {py_cls.__name__}: expected schema {self.schema}, got"
                    f" schema {value._fxtr_typespec.schema}"
                )
            raise ValidationError(f"Expected {py_cls.__name__}, got {type(value).__name__}")

        # Note: Validation of individual field values happens during DataEntity construction

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse DataEntity value by visiting its attributes."""
        # Visit each field value
        for field_name, field_type_spec in self.schema.field_type_specs.items():
            field_value = getattr(value, field_name)
            visitor.visit_part_at_path(
                py_path=(AccessPathStep_GetAttr(attr_name=field_name),),
                ipld_path=(AccessPathStep_GetItem_Str(key=field_name),),
                spec=field_type_spec,
                value=field_value,
            )

    def entity_data_to_ipld(self, entity: DataEntityBase) -> cbrrr.DagCborTypes:  # type: ignore
        """Convert DataEntityBase data to IPLD by serializing its attributes."""
        result = {}
        result["$typespec"] = self.self_to_ipld()

        # Serialize each attribute using its field type spec
        for field_name, field_type_spec in self.schema.field_type_specs.items():
            field_value = getattr(entity, field_name)
            result[field_name] = field_type_spec.value_to_ipld(field_value)

        return result

    def ipld_to_entity_object(
        self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> DataEntityBase:
        """Reconstruct DataEntityBase from IPLD representation."""
        if not isinstance(ipld_value, dict):
            raise ValidationError(
                f"Expected dict for DataEntityBase, got {type(ipld_value).__name__}"
            )

        # Note: $typespec key is already validated by the caller

        # Reconstruct each field from IPLD using its type spec
        field_data = {}
        for field_name, field_type_spec in self.schema.field_type_specs.items():
            if field_name not in ipld_value:
                raise ValidationError(f"Missing required field: {field_name}")
            field_ipld = ipld_value[field_name]
            field_data[field_name] = field_type_spec.ipld_to_value(field_ipld, lookup)

        # Get the Python class for this entity type and create an instance
        py_class = self._get_py_class()
        return py_class(**field_data)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse the schema."""
        visitor.visit_part_at_path(
            py_path=(AccessPathStep_GetAttr(attr_name="schema"),),
            ipld_path=(AccessPathStep_GetItem_Str(key="schema"),),
            spec=DataEntitySchema.typespec_cls(),
            value=self.schema,
        )

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert the type spec itself to IPLD by serializing its schema."""
        return {
            "$kind": "data_entity",
            "schema": self.schema.as_ipld_link(),
        }

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_DataEntity:
        """Reconstruct type spec from IPLD."""
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "data_entity", f"Expected data_entity kind"

        schema_ipld = ipld_value["schema"]
        assert isinstance(schema_ipld, cbrrr.CID)
        schema = lookup(schema_ipld)
        assert isinstance(schema, DataEntitySchema)
        return cls(schema=schema)


@dataclasses.dataclass(frozen=True)
class TypeSpec_DataEntityForwardRef(TypeSpecBase):
    """A forward reference to a data entity type, identified by namespace_id and type_id.

    This is not an entity typespec itself, but rather a placeholder that defers to the
    actual entity for serialization and only validates that the entity has the correct
    namespace_id and type_id.
    """

    namespace_id: str
    type_id: str

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        if isinstance(other, TypeSpec_DataEntityForwardRef):
            return self.namespace_id == other.namespace_id and self.type_id == other.type_id
        elif isinstance(other, TypeSpec_DataEntity):
            return (
                self.namespace_id == other.schema.namespace_id
                and self.type_id == other.schema.type_id
            )
        else:
            return False

    def validate(self, value: Any) -> None:
        """Validates that the value is a DataEntityBase with matching namespace_id and type_id."""
        if not isinstance(value, DataEntityBase):
            raise ValidationError(f"Expected DataEntityBase, got {type(value).__name__}")

        # Check that the entity's schema matches our forward reference
        entity_schema = value._fxtr_typespec.schema
        if entity_schema.namespace_id != self.namespace_id or entity_schema.type_id != self.type_id:
            raise ValidationError(
                f"Expected entity with namespace_id='{self.namespace_id}' and"
                f" type_id='{self.type_id}', but got entity with"
                f" namespace_id='{entity_schema.namespace_id}' and"
                f" type_id='{entity_schema.type_id}'"
            )

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Defer to the actual entity's typespec for traversal."""
        if not isinstance(value, DataEntityBase):
            raise ValidationError(f"Expected DataEntityBase, got {type(value).__name__}")

        visitor.visit_part_at_path(
            py_path=(),
            ipld_path=(),
            spec=value._fxtr_typespec,
            value=value,
        )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        """Defer to the actual entity's typespec for serialization."""
        if not isinstance(value, DataEntityBase):
            raise ValidationError(f"Expected DataEntityBase, got {type(value).__name__}")

        return value._fxtr_typespec.value_to_ipld(value)

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> DataEntityBase:
        """Look up the entity by CID and validate it matches our forward reference."""
        if not isinstance(ipld_value, cbrrr.CID):
            raise ValidationError(f"Expected CID for forward ref, got {type(ipld_value).__name__}")

        # Look up the entity by CID
        entity = lookup(ipld_value)
        assert isinstance(entity, DataEntityBase)
        self.validate(entity)
        return entity

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """TypeSpec_DataEntityForwardRef has no sub-structure to traverse."""

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Serialize the forward reference specification."""
        return {
            "$kind": "data_entity_fwdref",
            "namespace_id": self.namespace_id,
            "type_id": self.type_id,
        }

    @classmethod
    def ipld_to_this_type(cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Self:
        """Deserialize forward reference from IPLD."""
        if not isinstance(ipld_value, dict):
            raise ValidationError(
                f"Expected dict for TypeSpec_DataEntityForwardRef IPLD, got {type(ipld_value)}"
            )

        assert ipld_value["$kind"] == "data_entity_fwdref", f"Expected data_entity_fwdref kind"

        assert isinstance(ipld_value["namespace_id"], str)
        assert isinstance(ipld_value["type_id"], str)
        return cls(
            namespace_id=ipld_value["namespace_id"],
            type_id=ipld_value["type_id"],
        )


# endregion
####################################################################################################
# region: Interfaces and their type specifications
####################################################################################################


@dataclasses.dataclass(frozen=True, eq=False)
class InterfaceDefn[T](BuiltinEntityFromAnnotations, typespec_kind="interface_defn"):
    """The definition of a interface.

    T should be bound to the FxtrCallTarget type that this interface defn is for, without any
    bound type variables.

    Attributes:
        namespace_id: The namespace identifier for the namespace where the interface lives.
        interface_id: The identifier for the interface type.
        methods: A tuple of method names that are part of this interface.
        type_vars: A tuple of type variable names that are part of this interface.
        stateless: A boolean indicating whether this interface guarantees stateless behavior.
            If True, results of calls through this interface are independent and determined solely
            by their inputs, with no observable mutable state maintained between method calls, and
            either deterministic or intependently random results for each call. Instances can
            be freely shared between multiple users without coordination. If False, calls
            may interact and users should assume they need isolated instances. Note that only
            stateless interfaces can be directly referenced by fxtr plans; implementations of
            possibly-stateful plans must be constructed and returned by stateless "factory" objects.
            (Delays are not considered to be part of the observable state of the interface, so e.g.
            implementations may be considered stateless even if they perform internal rate
            limiting.)
        _doc: A documentation string for the entity schema. NOT considered part of the entity, and
            will not be serialized into IPLD or hashed to produce the entity's CID; this means that
            two schemas with the same namespace, type_id, and field types will be considered equal
            even if they have different documentation strings. (Note, however, that the semantic
            content of this docstring should usually be considered "part of the definition" of the
            interface, and by defining a service interface you promise not to change its semantic
            meaning. We allow docstring changes to enable e.g. adding extra useful information,
            describing issues with the current spec, or providing migration info.)
    """

    namespace_id: str
    interface_id: str
    methods: tuple[str, ...]
    type_vars: tuple[str, ...]
    stateless: bool
    _doc: str | None = dataclasses.field(compare=False, default=None)

    @classmethod
    def _field_typespecs(cls) -> dict[str, TypeSpecBase]:
        """Get the typespecs for the fields of this protocol entity itself."""
        my_specs = super()._field_typespecs()
        del my_specs["_doc"]
        return my_specs

    def bind(self, **bindings: Any) -> InterfaceSpec[Any]:
        return InterfaceSpec(defn=self, bindings=frozendict(bindings))


@dataclasses.dataclass(frozen=True)
class InterfaceSpec[T: FxtrCallTarget](
    BuiltinEntityFromAnnotations, typespec_kind="interface_spec"
):
    """The concrete specification of a interface with all type variables bound.

    Currently the bindings map to a fairly unstable hacky representation of the Python types that
    is not robust. This should be sufficient for different Python types to map to different
    interfaces, but the current values should be seen mostly like documentation or tagging rather
    than a full type-safe representation.

    This type will change significantly in the future.
    """

    defn: InterfaceDefn
    bindings: frozendict[str, AnyFrozenIPLDable]

    def define_external_service_impl(
        self, service_id: str, doc: str | None = None, *, namespace_id: str | None = None
    ) -> ExternalServiceIdentifier[T]:
        """Define an external service identifier for this protocol."""
        # Detect if this is called at the top level of a module
        namespace_id = _get_type_namespace_id(namespace_id)

        return ExternalServiceIdentifier(
            namespace_id=namespace_id,
            service_id=service_id,
            interface_spec=self,
            _doc=doc,
        )


@dataclasses.dataclass(frozen=True)
class OverrideReifiedType:
    """Annotation to override the reified type of a type variable.

    Usable as Annotated[Any, OverrideReifiedType(reified_type)]
    """

    reified_type: tuple[Any, ...]


class ConfiguredPlan[T: FxtrCallTarget](EntityBase, abc.ABC):
    """A plan for a dynamic interface spec stored as an instance variable."""

    interface_spec: InterfaceSpec[T]


class ComputedPlan[T: FxtrCallTarget](EntityBase, abc.ABC):
    """A plan whose interface is computed from the plan without being directly configured."""

    @property
    @abc.abstractmethod
    def interface_spec(self) -> InterfaceSpec[T]: ...


type Plan[T: FxtrCallTarget] = ConfiguredPlan[T] | ComputedPlan[T]
"""A plan entity, which can be instantiated as a call target with a particular interface spec."""


def is_abstract_call_target(obj: Any) -> bool:
    """Returns True if the object is an abstract call target, False otherwise."""
    return (
        isinstance(obj, type)
        and issubclass(obj, FxtrCallTarget)
        and "_fxtr_interface_defn" in obj.__dict__
    )


def interface_defn_of[T: FxtrCallTarget](cls: type[T]) -> InterfaceDefn[T]:
    return cls.__dict__["_fxtr_interface_defn"]


class ReifiedTypeTypeVarError(Exception):
    """Error raised when a type variable is not allowed."""


def _tuplify_type(
    ty: Any,
    tv_mode: Literal["forbid", "keep", "as_any"],
    tv_substitutions: dict[typing.TypeVar, Any] | None = None,
) -> Any:
    """Convert a Python type to a nested tuple of tags and values that represents its reified type.

    Args:
        ty: The Python type to convert
        tv_mode: What to do with type variables that are not explicitly substituted
        tv_substitutions: An optional dictionary of type variables to substitute explicitly.
            Substituted type variables are assumed to already be reified.

    Returns:
        A nested tuple of tags and values that represents the reified type
    """
    if isinstance(ty, str):
        return ("s", ty)
    elif isinstance(ty, typing.TypeVar):
        if tv_substitutions and ty in tv_substitutions:
            return tv_substitutions[ty]

        match tv_mode:
            case "as_any":
                return ("any",)
            case "forbid":
                raise ReifiedTypeTypeVarError(
                    f"Type variable {ty.__name__} is not allowed in this context"
                )
            case "keep":
                return ("tv", ty.__name__)
            case _:
                raise ValueError(f"Invalid TV mode: {tv_mode}")
    elif ty is Any:
        return ("any",)
    origin = typing.get_origin(ty) or ty
    args = typing.get_args(ty)
    if origin is typing.Annotated:
        match args:
            case [typing.Any, OverrideReifiedType(reified_type)]:
                return reified_type
            case _:
                raise NotImplementedError(
                    f"Annotated type {ty} cannot yet be reified into fxtr (we may expand this in"
                    " the future)"
                )
    if not args:
        if hasattr(ty, "__name__"):
            return ("n", ty.__name__)
        else:
            return ("z", repr(ty))
    else:
        return (
            "g",
            _tuplify_type(origin, tv_mode=tv_mode, tv_substitutions=tv_substitutions),
            tuple(
                _tuplify_type(arg, tv_mode=tv_mode, tv_substitutions=tv_substitutions)
                for arg in args
            ),
        )


class ReifiedTypeCheckError(Exception):
    """Error raised when a type check fails."""


def _tuplified_type_check_match(
    actual: tuple[Any, ...],
    expected: tuple[Any, ...],
    forgive_actual_any: bool,
    allow_expected_tv: bool,
    or_error: bool = False,
) -> bool:
    """Check if an actual (tuplified) type matches an expected (tuplified) type.

    Args:
        actual: The actual (tuplified) type to check
        expected: The expected (tuplified) type to check
        forgive_actual_any: If True, the type Any in the actual type will be allowed to match any
            specific other type even when we cannot check this.
        allow_expected_tv: If True, any type variables in the expected type will be allowed to
            match arbitrary other types.
        or_error: If True, an error will be raised if the types do not match.

    Returns:
        True if the types match, False otherwise
    """
    tag_actual = actual[0]
    tag_expected = expected[0]
    if tag_expected == "any":
        return True
    elif tag_expected == "tv":
        if allow_expected_tv:
            return True
        elif or_error:
            raise ReifiedTypeCheckError(
                f"Type variable {expected[1]} in expected type is unknown and cannot be checked!"
            )
        else:
            warnings.warn(
                f"Type variable {expected[1]} in expected type is unknown and cannot be checked!"
            )
            return False
    elif tag_actual == "any" or tag_actual == "tv":
        if forgive_actual_any:
            return True
        elif or_error:
            raise ReifiedTypeCheckError(
                f"Type variable {actual[1]} in actual type is unknown; cannot guarantee that it is"
                f" compatible with the expected type! Expected type debug dump: {expected}"
            )
        else:
            return False
    elif tag_actual in ("s", "n", "z"):
        if actual == expected:
            return True
        elif or_error:
            raise ReifiedTypeCheckError(
                f"Type {actual} in actual type does not match expected type {expected}!"
            )
        else:
            return False
    elif tag_actual == "g":
        if tag_expected != "g":
            if or_error:
                raise ReifiedTypeCheckError(
                    f"Type {actual} in actual type does not match expected type {expected}!"
                )
            else:
                return False
        actual_base, actual_args = actual[1:]
        expected_base, expected_args = expected[1:]
        try:
            if not _tuplified_type_check_match(
                actual_base,
                expected_base,
                forgive_actual_any=forgive_actual_any,
                allow_expected_tv=allow_expected_tv,
                or_error=or_error,
            ):
                return False
        except ReifiedTypeCheckError as e:
            e.add_note(f"In base type of {expected}")
            raise
        for i, (actual_arg, expected_arg) in enumerate(zip(actual_args, expected_args)):
            try:
                if not _tuplified_type_check_match(
                    actual_arg,
                    expected_arg,
                    forgive_actual_any=forgive_actual_any,
                    allow_expected_tv=allow_expected_tv,
                    or_error=or_error,
                ):
                    return False
            except ReifiedTypeCheckError as e:
                e.add_note(f"In argument {i} of {expected}")
                raise
        return True
    raise ValueError(f"Unrecognized tuplified types: {actual=}, {expected=}")


def _spec_check_match(
    actual: InterfaceSpec,
    expected: InterfaceSpec,
    forgive_actual_any: bool,
    allow_expected_tv: bool,
    or_error: bool = False,
) -> bool:
    """Check if an actual interface spec matches an expected interface spec.

    Args:
        actual: The actual interface spec to check
        expected: The expected interface spec to check
        forgive_actual_any: If True, the type Any in the actual interface spec will be allowed to match any
            specific other type even when we cannot check this.
        allow_expected_tv: If True, any type variables in the expected interface spec will be allowed to
            match arbitrary other types.
        or_error: If True, an error will be raised if the interface specs do not match.
    Returns:
        True if the interface specs match, False otherwise
    """
    if actual.defn != expected.defn:
        if or_error:
            raise ReifiedTypeCheckError(f"Expected {expected.defn}, got {actual.defn}")
        else:
            return False
    defn = actual.defn
    if set(actual.bindings.keys()) != set(defn.type_vars):
        raise RuntimeError(
            f"Internal error: expected {defn.interface_id} bindings to provide type variables"
            f" {defn.type_vars}, got {actual.bindings}"
        )
    if set(expected.bindings.keys()) != set(defn.type_vars):
        raise RuntimeError(
            f"Internal error: expected {defn.interface_id} bindings to provide type variables"
            f" {defn.type_vars}, got {expected.bindings}"
        )
    for k in defn.type_vars:
        try:
            if not _tuplified_type_check_match(
                actual=typing.cast(tuple[Any, ...], actual.bindings[k]),
                expected=typing.cast(tuple[Any, ...], expected.bindings[k]),
                forgive_actual_any=forgive_actual_any,
                allow_expected_tv=allow_expected_tv,
                or_error=or_error,
            ):
                return False
        except ReifiedTypeCheckError as e:
            e.add_note(f"In typevar binding {k} of {defn.interface_id}")
            raise
    return True


def _spec_of_with_mode[
    T: FxtrCallTarget
](
    ty: type[T] | types.GenericAlias,
    tv_mode: Literal["forbid", "keep", "as_any"],
    allow_impl: bool = False,
    tv_substitutions: dict[typing.TypeVar, Any] | None = None,
) -> InterfaceSpec[T]:
    origin = typing.get_origin(ty) or ty
    binding_names = getattr(origin, "__type_params__", ())
    args = typing.get_args(ty)
    if not isinstance(origin, type) or not issubclass(origin, FxtrCallTarget):
        raise TypeError(f"Type {ty.__name__} is not a subclass of FxtrCallTarget")
    if len(binding_names) != len(args):
        raise TypeError(
            f"Number of type variables ({len(binding_names)}: {binding_names}) does not match"
            f" number of arguments ({len(args)}) for {ty.__name__}; cannot automatically infer"
            " interface spec from type. If you want to allow any type, please explicitly"
            " parameterize the type with Any, e.g. MyGenericBase[Any]."
        )
    if "_fxtr_interface_defn" in origin.__dict__:
        defn = origin._fxtr_interface_defn
        defn_bindings = {
            str(binding_names[i]): _tuplify_type(
                args[i], tv_mode=tv_mode, tv_substitutions=tv_substitutions
            )
            for i in range(len(binding_names))
        }
    elif allow_impl:
        # Try to figure out the interface definition by walking the base classes.
        # For now, only looks at immediate base classes, not transitive ones.
        found_specs = []
        for base in types.get_original_bases(origin):
            base_origin = typing.get_origin(base) or base
            if (
                issubclass(base_origin, FxtrCallTarget)
                and base_origin is not FxtrCallTarget
                and hasattr(base_origin, "_fxtr_interface_defn")
            ):
                try:
                    new_tv_substitutions = {
                        binding_names[i]: _tuplify_type(
                            args[i], tv_mode=tv_mode, tv_substitutions=tv_substitutions
                        )
                        for i in range(len(binding_names))
                    }
                    logger.debug(
                        f"Substituting: From {ty} as"
                        f" {origin.__name__}[{', '.join(t.__name__ for t in binding_names)}] into"
                        f" {base} with subs {new_tv_substitutions}"
                    )
                    found_specs.append(
                        _spec_of_with_mode(
                            base, tv_mode=tv_mode, tv_substitutions=new_tv_substitutions
                        )
                    )
                except ReifiedTypeTypeVarError as e:
                    e.add_note(
                        "While trying to statically infer an interface spec of a concrete"
                        f" implementation type {ty} via its base class {base}"
                    )
                    raise
        if not found_specs:
            raise TypeError(
                f"Type {ty.__name__} does not have an interace definition or a base class with an"
                " interface definition; cannot statically infer an interface spec for this type!"
            )
        if len(found_specs) > 1:
            raise TypeError(
                f"Type {ty.__name__} has multiple base classes with interface definitions:"
                f" {found_specs}; cannot statically infer an interface spec for this type!"
            )
        return found_specs[0]
    else:
        raise TypeError(f"Type {ty.__name__} is not an interface definition")
    return InterfaceSpec(
        defn=defn,
        bindings=freeze(defn_bindings),
    )


def spec_of[T: FxtrCallTarget](ty: type[T] | types.GenericAlias) -> InterfaceSpec[T]:
    return _spec_of_with_mode(ty, tv_mode="forbid")


def spec_of_concrete_target[
    T: FxtrCallTarget
](ty: type[T] | types.GenericAlias,) -> InterfaceSpec[T]:
    return _spec_of_with_mode(ty, tv_mode="forbid", allow_impl=True)


@dataclasses.dataclass(frozen=True)
class _InferFromTypeSentinel:
    """Sentinel value for indicating that the interface spec should be inferred from the type."""

    def __call__(_inner_self, self: Any) -> InterfaceSpec:
        raise NotImplementedError("This sentinel value should not be called")


INFER_INTERFACE_FROM_BASE_CLASS = typing.cast(
    "Callable[..., InterfaceSpec]", _InferFromTypeSentinel()
)

type _AssumeSendable = SendableValue
"""Internal type annotation for a value that is assumed to be sendable."""


class FxtrCallTarget(abc.ABC):
    """Base class for callable types in fxtr.

    There are two types of call target class:
    - Abstract call targets, which define fxtr interfaces. These specify the methods, types, and
      associated rules for the object. All methods of an abstract call target (except
      interface_spec) must be abstract, async, and have arguments and a return type that are
      fxtr-sendable. Every abstract call target is associated with an interface_id, which (along
      with its namespace) uniquely identifies this type across the hub. Abstract call targets are
      allowed to have type parameters.
    - Concrete call targets, which implement a single abstract call target, and usually directly
      subclass the abstract call target they implement. Each instance of a concrete call target
      must be able to report its concrete interface type (the fxtr id of the interface definition
      along with any type variables) using the method `interface_spec`. Concrete call target types
      are allowed to have type variables, but each instance must be associated with a specific
      binding of those type variables.

    It is not a hard requirement that a concrete call target subclass the abstract call target it
    implements; the main purpose of doing so is to improve Python-side type checking. However, even
    if it is not directly a subclass, the concrete call target should be treated as a "virtual
    instance" of the abstract call target, and fxtr provides functions for casting it to the
    appropriate abstract type for the purposes of the Python type checker.

    Abstract call targets must declare whether or not implementations are required to be stateless.

    Class keyword arguments:
        interface_id: The identifier for the interface type.
        namespace_id: The namespace identifier for the namespace where the interface lives. If not
            provided, inferred from containing module or scope if possible.
        stateless: A boolean indicating whether this interface guarantees stateless behavior.
            If True, results of calls through this interface are independent and determined solely
            by their inputs, with no observable mutable state maintained between method calls, and
            either deterministic or intependently random results for each call. Instances can
            be freely shared between multiple users without coordination. If False, calls
            may interact and users should assume they need isolated instances. Note that only
            stateless interfaces can be directly referenced by fxtr plans; implementations of
            possibly-stateful plans must be constructed and returned by stateless "factory" objects.
            (Delays are not considered to be part of the observable state of the interface, so e.g.
            implementations may be considered stateless even if they perform internal rate
            limiting.)
        assumed_sendable_fwdrefs: Collection of string names for types that should be assumed to
            be sendable without further validation. Required if this class has methods that use
            forward-defined types in type annotations.
    """

    _fxtr_interface_defn: ClassVar[InterfaceDefn]

    @abc.abstractmethod
    def interface_spec(self) -> InterfaceSpec[Any]: ...

    def __init_subclass__(
        cls,
        *,
        interface_id: str | None | NotImplementedType = None,
        namespace_id: str | None = None,
        stateless: bool | None = None,
        has_mutable_state: bool | None = None,
        assumed_sendable_fwdrefs: Collection[str] | None = None,
        **kwargs,
    ):
        super().__init_subclass__(**kwargs)

        # Case 1: are we defining an abstract interface? This is the case if `interface_id` is set.
        if interface_id is not None:
            namespace_id = _get_type_namespace_id(namespace_id)

            if has_mutable_state is not None:
                if stateless is not None:
                    raise TypeError(
                        "Cannot provide both stateless and has_mutable_state for an interface"
                        " definition!"
                    )
                warnings.warn(
                    "'has_mutable_state' is deprecated; plase switch to 'stateless' instead.",
                    stacklevel=2,
                )
                stateless = not has_mutable_state

            if stateless is None:
                raise TypeError(
                    "Interface definitions must specify whether they allow implementations to have"
                    " mutable internal state, by setting the `stateless` class keyword"
                    f" argument. This was not set on {cls.__name__}."
                )

            method_names = []
            method_docs = []
            for name, value in inspect.getmembers(cls):
                # ignore special members
                if name in IGNORED_CLASS_DICT_KEYS:
                    continue

                if hasattr(FxtrCallTarget, name):
                    orig_value = getattr(FxtrCallTarget, name)
                    # ignore non-overridden methods
                    if value is orig_value:
                        continue

                    # ignore things that are actually non-overridden classmethods
                    if (
                        inspect.ismethod(value)
                        and inspect.ismethod(orig_value)
                        and value.__self__ is cls
                        and orig_value.__self__ is FxtrCallTarget
                        and value.__func__ is orig_value.__func__
                    ):
                        continue

                    logger.warning(f"POSSIBLY BAD override: {name}: {value} is not {orig_value}")

                if not inspect.isfunction(value):
                    raise TypeError(
                        f"Found non-method attribute {name} on {cls.__name__}; not allowed for"
                        " abstract call targets. Abstract call targets (those that set "
                        "interface_id) must only contain abstract (async) methods."
                    )

                if not inspect.iscoroutinefunction(value):
                    raise TypeError(
                        f"Method {name} on {cls.__name__} is not async. Methods on abstract call"
                        " targets must be async."
                    )

                if not getattr(value, "__isabstractmethod__", False):
                    raise TypeError(
                        f"Method {name} on {cls.__name__} is not abstract. All methods on abstract"
                        " call targets (those that set interface_id) must be abstract."
                    )

                if not interface_allowed_method_filter(name):
                    raise TypeError(
                        f"Method name {repr(name)} on {cls.__name__} is not allowed. Methods on"
                        " abstract call targets must be public names (non-underscore-prefixed"
                        " name, or __call__)."
                    )

                # Get type annotations
                orig_globals = sys.modules[cls.__module__].__dict__
                extended_globals = dict(orig_globals)
                extended_globals[cls.__name__] = typing.TypeAliasType(  # type: ignore
                    cls.__name__, _AssumeSendable, type_params=(typing.TypeVarTuple("Ts"),)
                )
                # Annotated[_AssumeSendable, cls.__name__]
                if assumed_sendable_fwdrefs is not None:
                    extended_globals.update(
                        {
                            fwdref: typing.TypeAliasType(  # type: ignore
                                fwdref, _AssumeSendable, type_params=(typing.TypeVarTuple("Ts"),)
                            )
                            # fwdref: Annotated[_AssumeSendable, fwdref]
                            for fwdref in assumed_sendable_fwdrefs
                        }
                    )
                locals_with_type_vars = {
                    str(k): Annotated[_AssumeSendable, k] for k in cls.__type_params__
                }
                try:
                    annotations = inspect.get_annotations(
                        value, globals=extended_globals, locals=locals_with_type_vars, eval_str=True
                    )
                except Exception as e:
                    raise TypeError(
                        f"Could not evaluate type annotations for {cls.__name__}.{name}: {e}"
                    ) from e

                # Check return type annotation
                if "return" not in annotations:
                    raise TypeError(
                        f"Method {cls.__name__}.{name} must have a return type annotation"
                    )

                # Check if return type is a valid SendableValue type
                try:
                    assert_sendable_value_type(annotations["return"])
                except TypeError as e:
                    raise TypeError(
                        f"Return type of {cls.__name__}.{name} must be a valid SendableValue"
                        f" type, but got {annotations['return']}"
                    ) from e

                # Check parameter types (skip 'self' parameter for protocols)
                sig = inspect.signature(value)
                param_items = list(sig.parameters.items())
                if param_items and param_items[0][0] == "self":
                    param_items = param_items[1:]

                for param_name, param in param_items:
                    if param.annotation == inspect.Parameter.empty:
                        raise TypeError(
                            f"Parameter '{param_name}' of {cls.__name__}.{name} must have a "
                            "type annotation"
                        )

                    # Check if parameter type is a valid SendableValue type
                    try:
                        assert_sendable_value_type(annotations[param_name])
                    except TypeError as e:
                        raise TypeError(
                            f"Parameter '{param_name}' type of {cls.__name__}.{name} must be"
                            f" a valid SendableValue type, but got {annotations[param_name]}"
                        ) from e

                # Add to method list
                method_names.append(name)

                # Format method signature for documentation
                sig_str = f"async def {name}{sig}:"
                method_doc = f"    {sig_str}"
                if value.__doc__:
                    # Add indented docstring
                    doc_lines = value.__doc__.strip().split("\n")
                    method_doc += "\n        " + "\n        ".join(doc_lines)
                method_docs.append(method_doc)

            # Build documentation string
            doc_parts = []
            if cls.__doc__:
                doc_parts.append(cls.__doc__.strip())

            if method_docs:
                doc_parts.append("\nMethods:")
                doc_parts.extend(method_docs)

            full_doc = "\n".join(doc_parts) if doc_parts else None

            cls._fxtr_interface_defn = InterfaceDefn(
                namespace_id=namespace_id,
                interface_id=interface_id,
                methods=tuple(method_names),
                type_vars=tuple(str(x) for x in cls.__type_params__),
                stateless=stateless,
                _doc=full_doc,
            )
        else:
            if stateless is not None:
                raise TypeError(
                    f"Cannot provide 'stateless' for {cls.__name__} because it is not an abstract"
                    " call target!"
                )
            if has_mutable_state is not None:
                raise TypeError(
                    f"Cannot provide 'has_mutable_state' for {cls.__name__} because it is not an"
                    " abstract call target!"
                )
            if assumed_sendable_fwdrefs is not None:
                raise TypeError(
                    f"Cannot provide assumed_sendable_fwdrefs for {cls.__name__} because it is not"
                    " an abstract call target!"
                )
            if namespace_id is not None:
                raise TypeError(
                    f"Cannot provide namespace_id for {cls.__name__} because it is not an abstract"
                    " call target!"
                )

        # Case 2: are we defining a concrete implementation? This is the case if the
        # `interface_spec` method is implemented.
        if "interface_spec" in cls.__dict__:
            interface_spec = cls.interface_spec
            if interface_spec is INFER_INTERFACE_FROM_BASE_CLASS:
                logger.info(
                    "Inferring interface spec from type. TODO make this more sophisticated."
                )
                found_specs = []
                for base in types.get_original_bases(cls):
                    base_origin = typing.get_origin(base) or base
                    if (
                        issubclass(base_origin, FxtrCallTarget)
                        and base_origin is not FxtrCallTarget
                        and hasattr(base_origin, "_fxtr_interface_defn")
                    ):
                        try:
                            found_specs.append(_spec_of_with_mode(base, tv_mode="forbid"))
                        except ReifiedTypeTypeVarError as e:
                            raise TypeError(
                                "Could not automatically infer interface spec from generic base"
                                f" class {base} with type parameters; please either implement"
                                " `interface_spec` to return a concrete spec, or use 'Any' in the"
                                " base class instead of type parameters."
                            ) from e
                if not found_specs:
                    raise TypeError(
                        f"Could not find base class with an interface specs for {cls.__name__}"
                    )
                if len(found_specs) > 1:
                    raise TypeError(
                        f"Found multiple base classes with interface specs for {cls.__name__}:"
                        f" {found_specs}; cannot determine which to use"
                    )
                cls._fxtr_inferred_interface_spec = found_specs[0]
                cls.interface_spec = lambda self: found_specs[0]


# Type alias for values that can be sent over RPC.
# Note that non-frozen mappings and sequences always serialize as dicts and lists, so you are
# not allowed to assume a specific type of mutable mapping or sequence. Mutable types are also
# always copied.
SendableValue = (
    str
    | bytes
    | int
    | bool
    | float
    | None
    | cbrrr.CID
    | frozendict[str, "SendableValue"]
    | dict[str, "SendableValue"]
    | Mapping[str, "SendableValue"]
    | tuple["SendableValue", ...]
    | list["SendableValue"]
    | Sequence["SendableValue"]
    | EntityBase
    | FxtrCallTarget
)


def assert_sendable_value_type(python_type: type | Any) -> None:
    """Assert that a Python type annotation is a valid SendableValue type.

    Args:
        python_type: A Python type annotation

    Raises:
        TypeError: If the type is not a valid SendableValue type
    """
    # Check if it's the SendableValue type alias itself, or Any. We assume that Any refers to
    # an unspecified SendableValue type.
    if (
        python_type == SendableValue
        or python_type == "SendableValue"
        or python_type is Any
        or python_type is _AssumeSendable
    ):
        return

    if isinstance(python_type, typing.TypeVar):
        # Assume typevars are sendable (for interfaces with type parameters)
        return

    # Check for TypedDict - these are allowed as they're essentially dicts with fixed keys
    # Must check before get_origin since TypedDict has special handling
    if typing.is_typeddict(python_type):
        # All values in the TypedDict must be sendable
        # Use get_type_hints with include_extras to properly resolve ForwardRefs and keep Annotated
        annotations = typing.get_type_hints(python_type, include_extras=True)
        for key, value_type in annotations.items():
            try:
                assert_sendable_value_type(value_type)
            except TypeError as e:
                e.add_note(f"TypedDict field '{key}' has invalid type: {value_type}")
                raise
        return

    # Check for dict[str, ...] or list[...]
    origin = typing.get_origin(python_type) or python_type
    args = typing.get_args(python_type)

    if origin in {dict, frozendict, typing.Mapping, collections.abc.Mapping}:
        if len(args) != 2:
            raise TypeError(
                f"Mapping/dict type must have exactly 2 type arguments, got {len(args)}"
            )
        key_type, value_type = args
        # Key must be str
        if key_type is not str:
            raise TypeError(f"Mapping/dict key type must be str, got {key_type}")
        # Recurse on value type
        try:
            assert_sendable_value_type(value_type)
        except TypeError as e:
            e.add_note(f"Mapping/dict value type is invalid: {value_type}")
            raise
        return

    elif origin is tuple:
        if len(args) == 2 and args[1] is ...:
            # Recurse on element type
            try:
                assert_sendable_value_type(args[0])
            except TypeError as e:
                e.add_note(f"Tuple element type is invalid: {args[0]}")
                raise
            return

        else:
            # Recurse on element types
            for i, arg in enumerate(args):
                try:
                    assert_sendable_value_type(arg)
                except TypeError as e:
                    e.add_note(f"Tuple element {i} type is invalid: {arg}")
                    raise
            return

    elif origin in {list, typing.Sequence, collections.abc.Sequence}:
        if len(args) != 1:
            raise TypeError(
                f"Sequence/list type must have exactly 1 type argument, got {len(args)}"
            )
        # Recurse on element type
        try:
            assert_sendable_value_type(args[0])
        except TypeError as e:
            e.add_note(f"Sequence/list element type is invalid: {args[0]}")
            raise
        return

    # Check for union types (including Optional[T])
    elif origin is typing.Union or origin is types.UnionType:
        # All union members must be sendable
        for arg in args:
            try:
                assert_sendable_value_type(arg)
            except TypeError as e:
                e.add_note(f"Union member type is invalid: {arg}")
                raise
        return

    elif origin is Annotated:
        try:
            assert_sendable_value_type(args[0])
        except TypeError as e:
            e.add_note(f"Annotated value type is invalid: {args[0]}")
            raise
        return

    # Check for generic aliases wrapping call targets (e.g., MyProtocol[T])
    elif origin is not None and isinstance(origin, type) and issubclass(origin, FxtrCallTarget):
        # All type arguments must be sendable
        for i, arg in enumerate(args):
            try:
                assert_sendable_value_type(arg)
            except TypeError as e:
                e.add_note(f"Generic call target type argument {i} is invalid: {arg}")
                raise
        return

    elif isinstance(origin, typing.TypeAliasType):
        # Desugar type aliases.
        try:
            assert_sendable_value_type(origin.__value__)
        except TypeError as e:
            e.add_note(f"Type alias {origin.__name__} value is invalid: {origin.__value__}")
            raise
        for tv, arg in zip(origin.__type_params__, args):
            try:
                assert_sendable_value_type(arg)
            except TypeError as e:
                e.add_note(f"Type alias {origin.__name__} type parameter {tv} is invalid: {arg}")
                raise
        return

    # Check if it's a CBOR-serializable primitive
    elif python_type in (str, bytes, int, bool, float, type(None), None, cbrrr.CID):
        return

    # Check if it's a subclass of FxtrCallTarget
    elif isinstance(python_type, type) and issubclass(python_type, FxtrCallTarget):
        return

    # Check if it's an entity type by trying to convert to TypeSpec_AnyEntity
    else:
        try:
            hub_type = python_type_to_hub_type_spec(python_type)
        except TypeAnnotationConversionError as e:
            raise TypeError(
                f"Type {python_type} is not a SendableValue type (cannot convert to hub type"
                f" spec)\nDebug info: origin type was {type(origin)}"
            ) from e
        # Check if TypeSpec_AnyEntity is a supertype
        any_entity_spec = TypeSpec_AnyEntity()
        if any_entity_spec.is_supertype_of(hub_type):
            return
        else:
            raise TypeError(f"Type {python_type} is not a SendableValue type (not an entity type)")


def interface_allowed_method_filter(method_name: str) -> bool:
    """Method filter: allow public methods (non-underscore-prefixed, except __call__)

    Allows any method that does NOT start with an underscore, and also allows __call__.
    This is used to determine which methods are exposed over RPC in protocol implementations.
    """
    return (not method_name.startswith("_")) or (method_name == "__call__")


def is_plan_for[
    T: FxtrCallTarget
](plan: Plan[Any], target_type: type[T], error_if_not: bool = False) -> TypeGuard[Plan[T]]:
    """Returns True if the plan can be instantiated as the given target type.

    Only returns true for exact matches, or situations where the target type has Any.

    Args:
        plan: The plan to check
        target_type: The target type to check
        error_if_not: If True, an error will be raised if the plan is not a plan for the given target type.

    Returns:
        True if the plan can be instantiated as the given target type, False otherwise
    """
    return _spec_check_match(
        actual=plan.interface_spec,
        expected=spec_of(target_type),
        forgive_actual_any=False,
        allow_expected_tv=False,
        or_error=error_if_not,
    )


def implements_spec[
    T: FxtrCallTarget
](
    impl: FxtrCallTarget,
    spec: InterfaceSpec[T],
    *,
    allow_untyped_impl: bool = False,
    error_if_not: bool = False,
) -> TypeGuard[T]:
    """Returns True if the implementation implements the given interface spec.

    Only returns true for exact matches, not supertypes.

    Args:
        impl: The implementation to check
        spec: The interface spec to check
        allow_untyped_impl: If True, and the implementation reports Any as part of its type
            specification, assume that the Any matches the requested type instead of failing.
        error_if_not: If True, an error will be raised if the implementation does not implement the given interface spec.

    Returns:
        True if the implementation implements the given interface spec, False otherwise
    """
    return _spec_check_match(
        actual=impl.interface_spec(),
        expected=spec,
        forgive_actual_any=allow_untyped_impl,
        allow_expected_tv=False,
        or_error=error_if_not,
    )


def implements_interface[
    T: FxtrCallTarget
](
    impl: FxtrCallTarget,
    target_type: type[T],
    *,
    allow_untyped_impl: bool = False,
    error_if_not: bool = False,
) -> TypeGuard[T]:
    """Returns True if the implementation implements the given target type.

    If target_type is a FxtrCallTarget class with no bound type parameters, we check if the
    implementation has the same interface definition. If target_type is a GenericAlias with bound
    type parameters, we also check if those type parameters exactly match the ones given by the
    call target. Only returns true for exact matches, not supertypes.

    Args:
        impl: The implementation to check
        target_type: The target type to check, which can either be a FxtrCallTarget class with no
            bound type parameters (e.g. TaskFunction), or a GenericAlias with bound type parameters
            (e.g. TaskFunction[SomeEntityType]).
        allow_untyped_impl: If True, and the implementation reports Any as part of its type
            specification, assume that the Any matches the requested type instead of failing.
        error_if_not: If True, an error will be raised if the implementation does not implement the given target type.

    Returns:
        True if the implementation implements the given target type, False otherwise
    """
    args = typing.get_args(target_type)
    if args:
        return _spec_check_match(
            actual=impl.interface_spec(),
            expected=spec_of(target_type),
            forgive_actual_any=allow_untyped_impl,
            allow_expected_tv=False,
            or_error=error_if_not,
        )
    else:
        matches = impl.interface_spec().defn == interface_defn_of(target_type)
        if error_if_not and not matches:
            raise TypeError(
                "Implementation does not implement the given target type: expected an"
                f" implementation of {interface_defn_of(target_type)}, got"
                f" {impl.interface_spec().defn}"
            )
        return matches


@dataclasses.dataclass(frozen=True, eq=False)
class ExternalServiceIdentifier[T: FxtrCallTarget](
    ConfiguredPlan[T],
    BuiltinEntityFromAnnotations,
    typespec_kind="external_service_identifier",
):
    """An identifier for an external service.

    External service identifiers are used to refer to services that are implemented by some system
    outside of the hub. Each instance of an external service identifier refers to a "spec-addressed"
    service, defined by reference to some externally-provided rules, such that any implementation of
    those rules can be provided as an implementation of the external service identifier.

    In general:
    - the individual/organization that publishes the external service identifier is responsible for
      clearly specifying the spec for an implementation of the service.
    - the individual/organization that provides an implementation of the external service identifier
      is responsible for ensuring that the implementation is compliant with the spec.

    Neither of these guarantees are explicitly enforced by the hub, but provenance is tracked, so
    that other organizations can potentially "vouch for" an implementation / "attest" that a
    provider provided the right service.

    External services are the foundation on which the hub can interface with the external world.
    Most operations on the hub are defined by reference to built-in external services, provided
    directly by the hub tooling.

    Attributes:
        namespace_id: The namespace identifier for the namespace where the service identifier lives.
        service_id: The namespace-unique identifier for the external service.
        interface_spec: The protocol spec that this service identifier implements.
        _doc: A documentation string for the entity schema. NOT considered part of the entity, and
            will not be serialized into IPLD or hashed to produce the entity's CID; this means that
            two schemas with the same namespace, service_id, and field types will be considered equal
            even if they have different documentation strings. (Note, however, that the semantic
            content of this docstring should usually be considered "part of the definition" of the
            identifier, and by defining a service identifier you promise not to change its semantic
            meaning. We allow docstring changes to enable e.g. adding extra useful information,
            describing issues with the current spec, or providing migration info.)
        _defined_in_module: The module in which this service identifier was defined, if known.
    """

    namespace_id: str
    service_id: str
    interface_spec: InterfaceSpec[T]
    _doc: str | None = dataclasses.field(compare=False, default=None)
    _defined_in_module: str | None = dataclasses.field(compare=False, repr=False, default=None)

    @classmethod
    def _field_typespecs(cls) -> dict[str, TypeSpecBase]:
        """Get the typespecs for the fields of this service identifier entity itself."""
        my_specs = super()._field_typespecs()
        del my_specs["_doc"]
        del my_specs["_defined_in_module"]
        return my_specs


@dataclasses.dataclass(frozen=True)
class TypeSpec_Plan(TypeSpecBase):
    """Type spec for interface implementation plans.

    This typespec includes (a) instances of Plan for this interface and
    (b) data entities whose schema entity has namespace "!builtin.interface_impl" and has a field
    "interface_spec" that is this interface.

    If interface_spec is None, this represents a typespec for implementation plans of any interface,
    making it a supertype of all specific interface implementation plans.
    """

    interface_spec: InterfaceSpec | None

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        if isinstance(other, TypeSpec_Plan):
            # If self.interface_spec is None, this is a supertype of any protocol implementation
            if self.interface_spec is None:
                return True
            # Otherwise, this is a supertype if it has the same protocol
            return self.interface_spec == other.interface_spec
        # TODO(daniel): This typespec morally includes a subset of ExternalServiceIdentifier and
        # DataEntity typespecs, but we can't guarantee that _every_ such instance is a valid
        # implementation of this typespec, because those typespecs are not specific enough. So, we
        # just declare that this is not a supertype of those types, despite having a nonzero
        # intersection with them. This _should_ be fine but worth double-checking depending on how
        # is_supertype_of is actually used.
        return False

    def validate(self, value: Any) -> None:
        """Raises a ValidationError if the value is not valid for this type."""
        if isinstance(value, ExternalServiceIdentifier):
            # If interface_spec is None, accept any protocol implementation
            if self.interface_spec is not None:
                try:
                    _spec_check_match(
                        actual=value.interface_spec,
                        expected=self.interface_spec,
                        forgive_actual_any=False,
                        allow_expected_tv=False,
                        or_error=True,
                    )
                except ReifiedTypeCheckError as e:
                    raise ValidationError(
                        "External service identifier has mismatched interface_spec: expected an"
                        f" implementation of {self.interface_spec}, got {value.interface_spec}"
                    ) from e
        elif isinstance(value, ConfiguredPlan | ComputedPlan):
            # If interface_spec is None, accept any protocol implementation
            if self.interface_spec is not None:
                try:
                    _spec_check_match(
                        actual=value.interface_spec,
                        expected=self.interface_spec,
                        forgive_actual_any=False,
                        allow_expected_tv=False,
                        or_error=True,
                    )
                except ReifiedTypeCheckError as e:
                    raise ValidationError(
                        f"Protocol implementation ({value.__class__.__name__}) has mismatched"
                        f" interface_spec: expected an implementation of {self.interface_spec}, got"
                        f" {value.interface_spec}"
                    ) from e
        elif isinstance(value, DataEntityBase):
            # Check if this is a data entity that matches our criteria
            schema = value._fxtr_typespec.schema
            if schema.namespace_id != "!builtin.protocol_impl":
                raise ValidationError(
                    "Unsupported protocol implementation type"
                    f" {type(value).__name__} ({schema.type_id}, {schema.namespace_id})"
                )
            assert hasattr(value, "interface_spec")
            vpspec = value.interface_spec  # type: ignore
            # If interface_spec is None, accept any protocol implementation
            if self.interface_spec is not None:
                try:
                    _spec_check_match(
                        actual=vpspec,
                        expected=self.interface_spec,
                        forgive_actual_any=False,
                        allow_expected_tv=False,
                        or_error=True,
                    )
                except ReifiedTypeCheckError as e:
                    raise ValidationError(
                        "Protocol implementation has mismatched interface_spec: expected an"
                        f" implementation of {self.interface_spec}, got {vpspec}"
                    ) from e
        else:
            raise ValidationError(
                f"Expected protocol implementation provider, got {type(value).__name__}"
            )

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse the protocol impl provider value."""
        # This value is an entity; delegate to its type spec
        assert isinstance(value, EntityBase)
        visitor.visit_part_at_path(
            py_path=(),
            ipld_path=(),
            spec=value._fxtr_typespec,
            value=value,
        )

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        """Convert protocol impl provider value to IPLD."""
        # Delegate to the value's actual type spec
        assert isinstance(value, EntityBase)
        return value._fxtr_typespec.value_to_ipld(value)

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        """Reconstruct protocol impl provider value from IPLD."""
        if not isinstance(ipld_value, cbrrr.CID):
            raise ValidationError(
                f"Expected CID for protocol impl provider deserialization, got {type(ipld_value)}"
            )

        # Look up the entity and validate it matches our criteria
        entity = lookup(ipld_value)
        self.validate(entity)
        return entity

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Traverse the protocol spec that defines this type."""
        if self.interface_spec is not None:
            visitor.visit_part_at_path(
                py_path=(AccessPathStep_GetAttr(attr_name="interface_spec"),),
                ipld_path=(AccessPathStep_GetItem_Str(key="interface_spec"),),
                spec=InterfaceSpec.typespec_cls(),
                value=self.interface_spec,
            )

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert the type spec itself to IPLD."""
        return {
            "$kind": "protocol_impl_plan",
            "interface_spec": (
                self.interface_spec.as_ipld_link() if self.interface_spec is not None else None
            ),
        }

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_Plan:
        """Reconstruct type spec from IPLD."""
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "protocol_impl_plan", f"Expected protocol_impl_plan kind"

        interface_spec_cid = ipld_value["interface_spec"]

        # Handle None case (any protocol)
        if interface_spec_cid is None:
            return cls(interface_spec=None)

        if not isinstance(interface_spec_cid, cbrrr.CID):
            raise ValidationError(
                f"Expected CID or None for interface_spec, got {type(interface_spec_cid)}"
            )

        interface_spec = lookup(interface_spec_cid)
        if not isinstance(interface_spec, InterfaceSpec):
            raise ValidationError(f"Expected InterfaceSpec, got {type(interface_spec).__name__}")

        return cls(interface_spec=interface_spec)


class PlanResolver(Protocol):
    """A function that can resolve a plan to an implementation.

    Used as an argument in plan implementations to provide dependencies for the plan.
    """

    def __call__[T: FxtrCallTarget](self, plan: Plan[T]) -> T: ...


# endregion
####################################################################################################
# region: AnyFrozenIPLDable type specification
####################################################################################################


@dataclasses.dataclass(frozen=True)
class TypeSpec_AnyFrozenIPLDable(TypeSpecBase):
    """Type spec for any frozen IPLD-serializable structure.

    Accepts primitives, bytes, entities, string-keyed frozendicts, and tuples containing
    any of these types recursively. Serializes directly to IPLD with entities as CID links.
    """

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        """Returns True if this type is a supertype of (e.g. includes all members of) the other type."""
        # Primitives
        if isinstance(other, TypeSpec_Primitive):
            return True  # All primitives are AnyFrozenIPLDable

        # Entities
        if isinstance(other, TypeSpecForSomeKindOfEntity):
            return True  # All entities are AnyFrozenIPLDable

        # String-keyed frozendict with AnyFrozenIPLDable values
        if isinstance(other, TypeSpec_FrozenDictStringKey):
            return self.is_supertype_of(other.value_type)

        # Entity-keyed frozendict - NOT a supertype since entity-keyed dicts aren't AnyFrozenIPLDable
        if isinstance(other, TypeSpec_FrozenDictEntityKey):
            return False

        # Variable-size tuple with AnyFrozenIPLDable elements
        if isinstance(other, TypeSpec_VariableSizeTuple):
            return self.is_supertype_of(other.repeated_type)

        # Fixed-size tuple with all AnyFrozenIPLDable elements
        if isinstance(other, TypeSpec_FixedSizeTuple):
            return all(self.is_supertype_of(item_type) for item_type in other.item_types)

        # Optional with AnyFrozenIPLDable inner type
        if isinstance(other, TypeSpec_Optional):
            return self.is_supertype_of(other.inner_type)

        # Union of entities - all entities are AnyFrozenIPLDable
        if isinstance(other, TypeSpec_UnionOfEntities):
            return True

        # Another AnyFrozenIPLDable
        if isinstance(other, TypeSpec_AnyFrozenIPLDable):
            return True

        return False

    def validate(self, value: Any) -> None:
        """Validates that the value is a valid AnyFrozenIPLDable."""
        # Primitives
        if value is None or isinstance(value, (str, int, float, bool, bytes)):
            return

        # Entities
        if isinstance(value, EntityBase):
            return  # Don't re-validate entity contents

        # Frozendict with string keys
        if isinstance(value, frozendict):
            for k, v in value.items():
                if not isinstance(k, str):
                    raise ValidationError(
                        "AnyFrozenIPLDable frozendict must have string keys, got"
                        f" {type(k).__name__}"
                    )
                try:
                    self.validate(v)
                except ValidationError as e:
                    e.add_note(f"In frozendict value for key {repr(k)}")
                    raise
            return

        # Tuple
        if isinstance(value, tuple):
            for i, item in enumerate(value):
                try:
                    self.validate(item)
                except ValidationError as e:
                    e.add_note(f"In tuple item {i}")
                    raise
            return

        # Not a valid AnyFrozenIPLDable type
        raise ValidationError(
            f"Value of type {type(value).__name__} is not a valid AnyFrozenIPLDable"
        )

    def traverse_value(self, value: Any, visitor: HubValueVisitor) -> None:
        """Traverse an AnyFrozenIPLDable value."""
        # Visit leaves as their own more specific typespec.
        if value is None or type(value) in (str, int, float, bool, bytes):
            visitor.visit_part_at_path(
                py_path=(),
                ipld_path=(),
                spec=TypeSpec_Primitive(primitive_type=type(value)),
                value=value,
            )
            return

        # Entities - use their actual type spec for traversal
        if isinstance(value, EntityBase):
            entity_spec = value._fxtr_typespec
            visitor.visit_part_at_path(
                py_path=(),
                ipld_path=(),
                spec=entity_spec,
                value=value,
            )
            return

        # Frozendict with string keys - traverse values with self as spec
        # So far we don't know a more specific spec (visitor can re-traverse to inspect)
        if isinstance(value, frozendict):
            for k, v in value.items():
                assert isinstance(
                    k, str
                ), f"Expected string key in frozendict, got {type(k).__name__}"
                visitor.visit_part_at_path(
                    py_path=(AccessPathStep_GetItem_Str(key=k),),
                    ipld_path=(AccessPathStep_GetItem_Str(key=k),),
                    spec=self,
                    value=v,
                )
            return

        # Tuple - traverse items with self as spec
        # So far we don't know a more specific spec (visitor can re-traverse to inspect)
        if isinstance(value, tuple):
            for i, item in enumerate(value):
                visitor.visit_part_at_path(
                    py_path=(AccessPathStep_GetItem_Int(key=i),),
                    ipld_path=(AccessPathStep_GetItem_Int(key=i),),
                    spec=self,
                    value=item,
                )
            return

        # Should not reach here if validation passed
        raise ValueError(f"Cannot traverse invalid AnyFrozenIPLDable type: {type(value)}")

    def value_to_ipld(self, value: Any) -> cbrrr.DagCborTypes:
        """Convert AnyFrozenIPLDable to IPLD representation."""
        # Primitives serialize directly
        if value is None or isinstance(value, (str, int, float, bool, bytes)):
            return value

        # Entities serialize as CID links
        if isinstance(value, EntityBase):
            return value._fxtr_cid

        # Frozendict with string keys
        if isinstance(value, frozendict):
            result = {}
            for k, v in value.items():
                result[k] = self.value_to_ipld(v)
            return result

        # Tuple
        if isinstance(value, tuple):
            result = []
            for item in value:
                result.append(self.value_to_ipld(item))
            return result

        # Should not reach here if validation passed
        raise ValueError(f"Cannot serialize invalid AnyFrozenIPLDable type: {type(value)}")

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Any:
        """Reconstruct AnyFrozenIPLDable from IPLD representation."""
        # Primitives
        if ipld_value is None or isinstance(ipld_value, (str, int, float, bool, bytes)):
            return ipld_value

        # CID links to entities
        if isinstance(ipld_value, cbrrr.CID):
            return lookup(ipld_value)

        # Dict (becomes frozendict)
        if isinstance(ipld_value, dict):
            result_items = {}
            for k, v in ipld_value.items():
                if not isinstance(k, str):
                    raise ValidationError(f"Expected string key in dict, got {type(k).__name__}")
                result_items[k] = self.ipld_to_value(v, lookup)
            return frozendict(result_items)

        # List (becomes tuple)
        if isinstance(ipld_value, list):
            result_items = []
            for item in ipld_value:
                result_items.append(self.ipld_to_value(item, lookup))
            return tuple(result_items)

        # Unknown IPLD type
        raise ValidationError(f"Unknown IPLD type: {type(ipld_value).__name__}")

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """AnyFrozenIPLDable type spec has no sub-structure to traverse."""

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        """Convert the type spec itself to IPLD."""
        return {"$kind": "any_frozen_ipldable"}

    @classmethod
    def ipld_to_this_type(
        cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup
    ) -> TypeSpec_AnyFrozenIPLDable:
        """Reconstruct type spec from IPLD."""
        assert isinstance(ipld_value, dict), f"Expected dict, got {type(ipld_value)}"
        assert ipld_value["$kind"] == "any_frozen_ipldable", f"Expected any_frozen_ipldable kind"
        return cls()


# endregion
####################################################################################################
# region: Hub type specification registry and meta type
####################################################################################################

TypeSpecForSomeKindOfEntity = (
    TypeSpecBase_SomeEntity
    | TypeSpec_AnyEntity
    | TypeSpec_UnionOfEntities
    | TypeSpec_AnyExternalServiceIdentifier
    | TypeSpec_Plan
    | TypeSpec_DataEntityForwardRef
)

# Global mapping from type kind strings to their implementing classes
HUB_TYPE_SPEC_REGISTRY: Mapping[str, type[TypeSpecBase]] = {
    "int": TypeSpec_Primitive,
    "float": TypeSpec_Primitive,
    "str": TypeSpec_Primitive,
    "bool": TypeSpec_Primitive,
    "none": TypeSpec_Primitive,
    "external_cid": TypeSpec_ExternalCID,
    "fixed_tuple": TypeSpec_FixedSizeTuple,
    "var_tuple": TypeSpec_VariableSizeTuple,
    "fdict_str": TypeSpec_FrozenDictStringKey,
    "fdict_cid": TypeSpec_FrozenDictCIDKey,
    "fdict_ent": TypeSpec_FrozenDictEntityKey,
    "fset_ent": TypeSpec_FrozenSetOfEntities,
    "optional": TypeSpec_Optional,
    "ent_union": TypeSpec_UnionOfEntities,
    "any_entity": TypeSpec_AnyEntity,
    "any_ext_svc_id": TypeSpec_AnyExternalServiceIdentifier,
    "any_frozen_ipldable": TypeSpec_AnyFrozenIPLDable,
    DataEntitySchema.typespec_cls.kind: DataEntitySchema.typespec_cls,
    "data_entity": TypeSpec_DataEntity,
    "data_entity_fwdref": TypeSpec_DataEntityForwardRef,
    InterfaceDefn.typespec_cls.kind: InterfaceDefn.typespec_cls,
    InterfaceSpec.typespec_cls.kind: InterfaceSpec.typespec_cls,
    ExternalServiceIdentifier.typespec_cls.kind: ExternalServiceIdentifier.typespec_cls,
    "protocol_impl_plan": TypeSpec_Plan,
}


def _register_hub_type_spec(kind: str, type_spec: type[TypeSpecBase]) -> None:
    """Register a type spec with the global registry."""
    assert isinstance(HUB_TYPE_SPEC_REGISTRY, dict)
    HUB_TYPE_SPEC_REGISTRY[kind] = type_spec


T_BuiltinEntity = typing.TypeVar("T_BuiltinEntity", bound=BuiltinEntityBase)


def _register_builtin_entity_type_spec(entity_cls: type[T_BuiltinEntity]) -> type[T_BuiltinEntity]:
    """Register a builtin entity type spec with the global registry."""
    assert isinstance(HUB_TYPE_SPEC_REGISTRY, dict)
    HUB_TYPE_SPEC_REGISTRY[entity_cls.typespec_cls.kind] = entity_cls.typespec_cls
    return entity_cls


class TypeSpec_TypeSpec(TypeSpecBase):
    """The type-spec for type-specs.

    This is the type spec that allows us to serialize type specs themselves to IPLD.
    """

    def is_supertype_of(self, other: TypeSpecBase) -> bool:
        return other == self

    def validate(self, value: Any) -> None:
        if not isinstance(value, TypeSpecBase):
            raise ValidationError(f"Expected a type spec, got {type(value).__name__}")

    def traverse_value(self, value: TypeSpecBase, visitor: HubValueVisitor) -> None:
        value.traverse_self(visitor)

    def value_to_ipld(self, value: TypeSpecBase) -> cbrrr.DagCborTypes:
        return value.self_to_ipld()

    def ipld_to_value(self, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> TypeSpecBase:
        """Deserializes a type specification from an IPLD object model representation."""
        if not isinstance(ipld_value, dict) or "$kind" not in ipld_value:
            raise ValidationError(f"Expected dict with 'kind' field, got {ipld_value}")

        kind = ipld_value["$kind"]
        assert isinstance(kind, str)
        if kind not in HUB_TYPE_SPEC_REGISTRY:
            raise ValidationError(f"Unknown type kind: {kind}")

        type_class = HUB_TYPE_SPEC_REGISTRY[kind]
        return type_class.ipld_to_this_type(ipld_value, lookup)

    def traverse_self(self, visitor: HubValueVisitor) -> None:
        """Instances of TypeSpec_TypeSpec have no sub-structure to traverse."""

    def self_to_ipld(self) -> cbrrr.DagCborTypes:
        return {"$kind": "type_spec"}

    @classmethod
    def ipld_to_this_type(cls, ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> Self:
        assert isinstance(ipld_value, dict)
        assert ipld_value["$kind"] == "type_spec", f"Expected type_spec kind"
        return cls()


def ipld_to_type_spec(ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> TypeSpecBase:
    """Deserializes a type specification from an IPLD object model representation.

    Args:
        ipld_value: An IPLD object model representation of a TypeSpec (from DAG-CBOR decoding,
            should have "$kind" field)
        lookup: Object that can load entities by their CID.

    Returns:
        A type specification.
    """
    return TypeSpec_TypeSpec().ipld_to_value(ipld_value, lookup)


def ipld_entity_data_to_entity(ipld_value: cbrrr.DagCborTypes, lookup: EntityLookup) -> AnyEntity:
    """Deserializes an entity's data from an IPLD object model representation.

    First deserializes the typespec object, then uses that to deserialize the entity data.

    Note: This function is the main way to deserialize top-level entity objects themselves from
    their data. Functions like `TypeSpec_AnyEntity().ipld_to_value` are instead used for
    deserializing _links_ to entities as they would appear in schemas, not the actual entity
    contents.

    Args:
        ipld_value: An IPLD object model representation of an entity's contents (from DAG-CBOR
            decoding, should have "$typespec" field)
        lookup: Object that can load entities by their CID.

    Returns:
        The entity whose data is stored in the ipld_value.
    """
    if not isinstance(ipld_value, dict) or "$typespec" not in ipld_value:
        raise ValidationError(f"Expected dict with '$typespec' field, got {ipld_value}")

    typespec_ipld = ipld_value["$typespec"]
    typespec = ipld_to_type_spec(typespec_ipld, lookup)
    assert isinstance(typespec, TypeSpecBase_SomeEntity)
    return typespec.ipld_to_entity_object(ipld_value, lookup)


# endregion
####################################################################################################
# region: Conversion between Python type annotations and hub type specs
####################################################################################################

_DATA_ENTITY_FWDREF_OVERRIDES: contextvars.ContextVar[Collection[EntityForwardRefMetadata]] = (
    contextvars.ContextVar("data_entity_fwdref_overrides", default={})
)


@contextlib.contextmanager
def _with_local_data_entity_fwdref_overrides(
    overrides: Collection[EntityForwardRefMetadata],
) -> Iterator[None]:
    combined = set(_DATA_ENTITY_FWDREF_OVERRIDES.get()) | set(overrides)
    token = _DATA_ENTITY_FWDREF_OVERRIDES.set(combined)
    try:
        yield
    finally:
        _DATA_ENTITY_FWDREF_OVERRIDES.reset(token)


def python_type_to_hub_type_spec(python_type: type | Any) -> TypeSpecBase:
    """Convert a Python type annotation to a hub type specification.

    Args:
        python_type: A Python type annotation
        type_var_mapping: A mapping of type variables to their corresponding hub type specifications

    Returns:
        The corresponding hub type specification

    Raises:
        TypeAnnotationConversionError: If the type cannot be converted
    """
    # Handle primitive types
    if python_type is int:
        return TypeSpec_Primitive(primitive_type=int)
    elif python_type is float:
        return TypeSpec_Primitive(primitive_type=float)
    elif python_type is str:
        return TypeSpec_Primitive(primitive_type=str)
    elif python_type is bool:
        return TypeSpec_Primitive(primitive_type=bool)
    elif python_type is type(None) or python_type is None:
        return TypeSpec_Primitive(primitive_type=type(None))

    # Handle external CID type
    elif python_type is cbrrr.CID:
        return TypeSpec_ExternalCID()

    # Handle any-entity, and any-typespec types
    elif python_type is AnyEntity:
        return TypeSpec_AnyEntity()
    elif python_type is TypeSpecBase:
        return TypeSpec_TypeSpec()

    # Handle builtin entity types.
    elif (
        isinstance(python_type, type)
        and issubclass(python_type, BuiltinEntityBase)
        and hasattr(python_type, "typespec_cls")
    ):
        return python_type.typespec_cls()

    # Handle type variables
    elif isinstance(python_type, typing.TypeVar):
        if python_type.__bound__:
            return python_type_to_hub_type_spec(python_type.__bound__)
        else:
            raise TypeAnnotationConversionError(
                f"Type variable {python_type} does not have a bound"
            )

    # Handle AnyFrozenIPLDable
    # (note: equality check does a small amount of normalization)
    elif python_type == AnyFrozenIPLDable:
        return TypeSpec_AnyFrozenIPLDable()

    # Handle generic types
    origin = typing.get_origin(python_type)
    args = typing.get_args(python_type)

    if origin is tuple:
        if not args:
            raise TypeAnnotationConversionError(
                "Tuple type must have type arguments in hub schemas"
            )

        # Check if it's a variable-length tuple like tuple[int, ...]
        if len(args) == 2 and args[1] is ...:
            repeated_type = python_type_to_hub_type_spec(args[0])
            return TypeSpec_VariableSizeTuple(repeated_type=repeated_type)
        else:
            # Fixed-size tuple
            item_types = tuple(python_type_to_hub_type_spec(arg) for arg in args)
            return TypeSpec_FixedSizeTuple(item_types=item_types)

    elif origin is frozendict or python_type is frozendict:
        if not args:
            raise TypeAnnotationConversionError(
                "frozendict type must have type arguments in hub schemas"
            )
        if len(args) != 2:
            raise TypeAnnotationConversionError(
                "frozendict type must have exactly 2 type arguments in hub schemas"
            )

        key_type_py, value_type_py = args
        value_type = python_type_to_hub_type_spec(value_type_py)

        if key_type_py is str:
            return TypeSpec_FrozenDictStringKey(value_type=value_type)
        elif key_type_py is cbrrr.CID or typing.get_origin(key_type_py) == CID_Of:
            return TypeSpec_FrozenDictCIDKey(value_type=value_type)
        else:
            # Try to convert key type to entity type
            key_type = python_type_to_hub_type_spec(key_type_py)
            if not isinstance(key_type, TypeSpecForSomeKindOfEntity):
                raise TypeAnnotationConversionError(
                    "frozendict keys must be str, CID, or entity types in hub schemas, got"
                    f" {key_type_py}"
                )
            return TypeSpec_FrozenDictEntityKey(key_type=key_type, value_type=value_type)

    elif origin is frozenset or python_type is frozenset:
        if not args:
            raise TypeAnnotationConversionError(
                "frozenset type must have type arguments in hub schemas"
            )
        if len(args) != 1:
            raise TypeAnnotationConversionError(
                "frozenset type must have exactly 1 type argument in hub schemas"
            )

        item_type_py = args[0]
        item_type = python_type_to_hub_type_spec(item_type_py)

        # Check if it's an entity type
        if not isinstance(item_type, TypeSpecForSomeKindOfEntity):
            raise TypeAnnotationConversionError(
                f"frozenset items must be entity types in hub schemas, got {item_type_py} (which"
                f" converts to {item_type})"
            )

        return TypeSpec_FrozenSetOfEntities(item_type=item_type)

    elif origin is types.UnionType or origin is typing.Union:
        # Handle union types like int | str or str | None

        # Separate None from other types
        none_args = [arg for arg in args if arg is type(None)]
        non_none_args = [arg for arg in args if arg is not type(None)]

        # Check for malformed unions with multiple None
        if len(none_args) > 1:
            raise TypeAnnotationConversionError(
                f"Malformed union type with multiple None: {python_type}"
            )

        has_none = len(none_args) == 1

        # Handle the non-None part
        if len(non_none_args) == 1:
            # Single type (possibly with None)
            inner_type = python_type_to_hub_type_spec(non_none_args[0])
        elif len(non_none_args) > 1:
            # Multiple types - must all be entities for union
            entity_types = set()
            for arg in non_none_args:
                arg_spec = python_type_to_hub_type_spec(arg)
                if not isinstance(arg_spec, TypeSpecBase_SomeEntity):
                    raise TypeAnnotationConversionError(
                        "Union types can only contain entity types in hub schemas, but union type"
                        f" {python_type} contains non-entity {arg}"
                    )
                entity_types.add(arg_spec)
            inner_type = TypeSpec_UnionOfEntities(entity_types=frozenset(entity_types))
        else:
            raise TypeAnnotationConversionError(f"Empty union type: {python_type}")

        # Wrap in Optional if None was present
        if has_none:
            return TypeSpec_Optional(inner_type=inner_type)
        else:
            return inner_type

    elif origin is Annotated:
        # Handle Annotated types like Annotated[Struct, StructFieldTypes(...)]
        if not args:
            raise TypeAnnotationConversionError("Annotated type must have arguments")

        base_type = args[0]
        all_annotations = args[1:]

        # Filter out annotations we don't understand, keeping only known types
        known_annotations = []
        for annotation in all_annotations:
            if isinstance(annotation, (EntityForwardRefMetadata, HasTypeSpec)):
                known_annotations.append(annotation)

        # If no known annotations remain, fall back to converting the base type
        if not known_annotations:
            return python_type_to_hub_type_spec(base_type)

        # Use the filtered annotations for the rest of the logic
        annotations = known_annotations

        if base_type is DataEntityBase:
            # Require exactly one EntityForwardRefMetadata annotation
            if len(annotations) != 1:
                raise TypeAnnotationConversionError(
                    "Annotated[DataEntityBase, ...] must have exactly one annotation, but got"
                    f" {len(annotations)}"
                )

            annotation = annotations[0]
            if not isinstance(annotation, EntityForwardRefMetadata):
                raise TypeAnnotationConversionError(
                    "Annotated[DataEntityBase, ...] must have exactly one EntityForwardRefMetadata"
                    f" annotation, but got: {type(annotation)}"
                )

            return TypeSpec_DataEntityForwardRef(
                namespace_id=annotation.namespace_id,
                type_id=annotation.type_id,
            )
        elif base_type is Any:
            # Validate that all annotations are HasTypeSpec
            has_type_spec_annotations = []
            for annotation in annotations:
                if isinstance(annotation, HasTypeSpec):
                    has_type_spec_annotations.append(annotation)
                else:
                    raise TypeAnnotationConversionError(
                        "Annotated[Any, ...] must only contain HasTypeSpec annotations, "
                        f"but found: {type(annotation)}"
                    )

            if not has_type_spec_annotations:
                raise TypeAnnotationConversionError(
                    "Annotated[Any, ...] must include at least one HasTypeSpec annotation"
                )

            # Validate that adjacent annotations form a supertype chain
            # (earlier typespec must be a supertype of later one)
            for i in range(len(has_type_spec_annotations) - 1):
                earlier_typespec = has_type_spec_annotations[i].typespec
                later_typespec = has_type_spec_annotations[i + 1].typespec
                if not earlier_typespec.is_supertype_of(later_typespec):
                    raise TypeAnnotationConversionError(
                        "Invalid supertype chain in Annotated[Any, ...]: "
                        f"annotation at position {i} ({type(earlier_typespec).__name__}) "
                        f"is not a supertype of annotation at position {i + 1} "
                        f"({type(later_typespec).__name__})"
                    )

            # Return the most specific (last) typespec
            return has_type_spec_annotations[-1].typespec
        else:
            # Base type must be Struct, DataEntityBase, or Any
            raise TypeAnnotationConversionError(
                f"Annotated base type must be Struct, DataEntityBase, or Any, but got: {base_type}"
            )

    elif origin in {Plan, ConfiguredPlan, ComputedPlan}:
        if len(args) != 1:
            raise TypeAnnotationConversionError("Plan type must have one argument")

        if args[0] is Any or isinstance(args[0], typing.TypeVar):
            return TypeSpec_Plan(interface_spec=None)
        else:
            return TypeSpec_Plan(interface_spec=_spec_of_with_mode(args[0], tv_mode="as_any"))

    elif origin == CID_Of:
        if len(args) != 1:
            raise TypeAnnotationConversionError("CID_Of type must have one argument")
        return TypeSpec_ExternalCID()

    elif isinstance(origin, type) and issubclass(origin, EntityBase):
        return python_type_to_hub_type_spec(origin)

    # Handle entity types (classes with value_typespec attribute)
    if isinstance(python_type, UserDefinedEntityMeta) and hasattr(python_type, "value_typespec"):
        overrides = _DATA_ENTITY_FWDREF_OVERRIDES.get()
        data_entity_typespec = python_type.value_typespec
        if isinstance(data_entity_typespec, TypeSpec_DataEntity):
            namespace_id = data_entity_typespec.schema.namespace_id
            type_id = data_entity_typespec.schema.type_id
            if EntityForwardRefMetadata(namespace_id=namespace_id, type_id=type_id) in overrides:
                return TypeSpec_DataEntityForwardRef(namespace_id=namespace_id, type_id=type_id)
        return python_type.value_typespec

    # Reject Any and other unsupported types
    if python_type is Any:
        raise TypeAnnotationConversionError("Any type is not supported in hub schemas")

    if origin is dict or python_type is dict:
        raise TypeAnnotationConversionError(
            "dict type is not supported in hub schemas (due to being mutable); consider using"
            " frozendict instead"
        )

    if origin is list or python_type is list:
        raise TypeAnnotationConversionError(
            "list type is not supported in hub schemas (due to being mutable); consider using"
            " variable-length tuple instead"
        )

    raise TypeAnnotationConversionError(f"Unsupported Python type: {repr(python_type)}.")


def hub_type_spec_to_python_type(hub_type: TypeSpecBase) -> Any:
    """Convert a hub type specification to a Python type annotation.

    Args:
        hub_type: A hub type specification

    Returns:
        The corresponding Python type annotation

    Raises:
        TypeAnnotationConversionError: If the type cannot be converted
    """
    if isinstance(hub_type, TypeSpec_Primitive):
        if hub_type.primitive_type is type(None):
            return None
        return hub_type.primitive_type

    elif isinstance(hub_type, TypeSpec_ExternalCID):
        return cbrrr.CID

    elif isinstance(hub_type, TypeSpec_AnyEntity):
        return AnyEntity

    elif isinstance(hub_type, TypeSpec_AnyExternalServiceIdentifier):
        return ExternalServiceIdentifier

    elif isinstance(hub_type, TypeSpec_TypeSpec):
        return TypeSpecBase

    elif isinstance(hub_type, TypeSpec_AnyFrozenIPLDable):
        return AnyFrozenIPLDable

    elif isinstance(hub_type, TypeSpec_DataEntityForwardRef):
        # Convert forward reference back to Annotated[DataEntityBase, EntityForwardRefMetadata(...)]
        forward_ref_metadata = EntityForwardRefMetadata(
            namespace_id=hub_type.namespace_id, type_id=hub_type.type_id
        )
        return Annotated[DataEntityBase, forward_ref_metadata]

    elif isinstance(hub_type, TypeSpec_FixedSizeTuple):
        item_types = [hub_type_spec_to_python_type(item_type) for item_type in hub_type.item_types]
        # tuple[a, b, c] is equivalent to tuple[(a, b, c)] due to how __getitem__ works
        return tuple[tuple(item_types)]

    elif isinstance(hub_type, TypeSpec_VariableSizeTuple):
        repeated_type = hub_type_spec_to_python_type(hub_type.repeated_type)
        return tuple[repeated_type, ...]

    elif isinstance(hub_type, TypeSpec_FrozenDictStringKey):
        value_type = hub_type_spec_to_python_type(hub_type.value_type)
        return frozendict[str, value_type]

    elif isinstance(hub_type, TypeSpec_FrozenDictCIDKey):
        value_type = hub_type_spec_to_python_type(hub_type.value_type)
        return frozendict[cbrrr.CID, value_type]

    elif isinstance(hub_type, TypeSpec_FrozenDictEntityKey):
        key_type = hub_type_spec_to_python_type(hub_type.key_type)
        value_type = hub_type_spec_to_python_type(hub_type.value_type)
        return frozendict[key_type, value_type]

    elif isinstance(hub_type, TypeSpec_FrozenSetOfEntities):
        item_type = hub_type_spec_to_python_type(hub_type.item_type)
        return frozenset[item_type]

    elif isinstance(hub_type, TypeSpec_Optional):
        inner_python_type = hub_type_spec_to_python_type(hub_type.inner_type)
        return inner_python_type | None

    elif isinstance(hub_type, TypeSpec_UnionOfEntities):
        entity_types = [hub_type_spec_to_python_type(et) for et in hub_type.entity_types]
        if len(entity_types) == 1:
            return entity_types[0]
        # Create union type using | operator
        result = entity_types[0]
        for et in entity_types[1:]:
            result = result | et
        return result

    elif isinstance(hub_type, TypeSpecBase_SomeBuiltinEntity) and hasattr(hub_type, "entity_cls"):
        # Convert built-in entity types to their Python equivalents.
        return hub_type.entity_cls

    elif isinstance(hub_type, TypeSpec_DataEntity):
        # Convert user-defined entity and service identifier types to their Python equivalents.
        return hub_type._get_py_class()

    elif isinstance(hub_type, TypeSpec_Plan):
        # TODO: support more than just this?
        return Annotated[Any, HasTypeSpec(typespec=hub_type)]

    else:
        raise TypeAnnotationConversionError(f"Unsupported hub type: {type(hub_type)}")


# endregion
####################################################################################################
# region: Tasks and runs
####################################################################################################


class StorageHandle[V: EntityBase](
    FxtrCallTarget,
    interface_id="StorageHandle^1",
    namespace_id="!builtin.tasks",
    stateless=True,
):
    """A handle that can retrieve some entity from storage.

    This is a low-level internal service used internally by the hub. You should generally NOT
    directly implement this service or construct a provider for it except via the `result_handle`
    method on a Task object. Other ways of constructing a StorageHandle may be added in the future.

    This method must deterministically return a constant result referred to by the handle. The
    exact value returned must be tracked by the provider of the handle implementation in the
    provenance record for the implementation. (For instance, for a TaskResultHandle, the provenance
    should specify which run of the task we are producing the result for.)

    By convention, plans for StorageHandle implementations should specify their contents precisely
    enough to identify an "equivalence class" of entities that are all acceptable to the consumer
    of the handle. For instance:
    - tasks expose a StorageHandle for their result, which uniquely identifies the task; this is
      enough in principle to re-run that task, and users of the result_handle are expected to allow
      any of these entities to be given as input.
    - eventually, if users directly upload data to the hub, the StorageHandle for that data could
      be a cryptographic hash of the data, which would ensure that the data is not corrupted; users
      of this handle can then assume that the data is exactly what was uploaded and tagged in this
      way
    - it might also make sense to expose a StorageHandle for e.g. files in Git commits, in which
      case the plan should be some sort of permalink and git hash.
    """

    @abc.abstractmethod
    async def retrieve(self) -> V:
        """Retrieve the entity from storage."""
        ...


class ImplProvenanceSet(
    DataEntityBase, type_id="ImplProvenanceSet^1", namespace_id="!builtin.task_metadata"
):
    """Python representation of an implementation provenance set.

    ImplProvenanceSet entities are used to store a record of how every implementation plan in
    a run was actually resolved to an implementation. This encompasses all of:
    - how handles to previous tasks were resolved to task runs
    - how handles to external services were resolved to service implementations
    - how handles to internally-tracked implementations were built (e.g. lockfiles for them)

    Each record is populated by the provider of the implementation plan.

    There are not specific restrictions on the type of a provenance record in general, to allow
    future flexibility. However, the records must follow these rules:

    - The record must uniquely identify the provider of the implementation plan (i.e. given the
      record, the fxtr library needs to be able to look up the provider).
    - Any dependencies that were resolved dynamically when providing the implementation should be
      tracked in some way in the record.
    - If a record produced by a previous instantiation is given to the provider as input, the
      provider should use the same version of the dependencies that were used in the original
      instantiation.
    - If a record is provided to the provider, the provider MUST ensure that it does NOT resolve a
      dependency to something different than what was specified in the record. If it cannot use the
      same version specified in the record, it MAY raise an informative error message instead of
      continuing.

    What counts as "a dependency" is intentionally left undefined, as it may depend on the specific
    provider. Examples might include e.g. specific versions of python packages, hashes of
    downloaded wheels or docker containers, etc. It is acceptable to omit tracking of certain
    dependencies that are not feasible to track, but providers should generally be designed to
    track every dependency that would reasonably be expected to be pinned in a lockfile.

    Attributes:
        provenances: A dictionary of provenance records, keyed by the CID of the implementation
            plan.
    """

    provenances: frozendict[CID_Of[Plan[Any]], AnyEntity]


class AdHocMetadata(
    DataEntityBase, type_id="AdHocMetadata^1", namespace_id="!builtin.task_metadata"
):
    """Python representation of an ad-hoc metadata entity.

    Ad-hoc metadata entities can store arbitrary JSON-compatible data and reference arbitrary
    entities. This type is designed to store free-form metadata that has not yet been standardized,
    and as an "escape hatch" for tracking any information that might be useful to a user.

    This type does NOT impose any semantic restrictions on the data it contains. When this type is
    used inside another entity schema, consumers of that container entity should NOT require or
    assume that the data in the AdHocMetadata has any particular structure. If a schema definer
    wants to impose additional validity constraints on the data, they should define a new entity
    type instead of using AdHocMetadata directly.

    (Consumers of AdHocMetadata or a container entity _may_ check if the AdHocMetadata contains a
    reference to a specific other entity with a known schema and interpretation. In this case, the
    consumer is free to assume that this other entity follows the rules defined for that entity.
    However, consumers of AdHocMetadata should be aware that any such entity may be missing, and
    should be prepared to ignore parts of the AdHocMetadata that they do not recognize. The
    main use case for this is to embed application-specific tags or metadata into a larger
    freeform structure.)

    Attributes:
        metadata: Arbitrary immutable metadata containing JSON-like objects and entities.
    """

    metadata: AnyFrozenIPLDable


class BasicRunInfo(DataEntityBase, type_id="BasicRunInfo^1", namespace_id="!builtin.task_metadata"):
    """Python representation of a basic run info entity.

    Basic run info entities are used to store basic info about a run execution. We keep this
    separate from RunSpec for now because we may want to track different metadata for different
    runs in the future, without changing the core RunSpec.

    Attributes:
        author: The name of the author of the run.
        author_email: The email of the author of the run.
        timestamp: The timestamp of the run. Must be a string in RFC 3339 format.
        message: A free-form message about the run.
        metadata: Metadata about the run.
    """

    author: str
    author_email: str
    timestamp: str
    message: str | None
    metadata: AdHocMetadata


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class ConstantHandle[R: EntityBase](
    ComputedPlan[StorageHandle[R]],
    BuiltinEntityFromAnnotations,
    typespec_kind="constant_handle",
):
    """Handle for the service that provides a constant value.

    This is a hard-coded implementation plan that indicates that the hub tooling should provide a
    constant value.

    Attributes:
        value: The constant value to provide.
    """

    value: R

    @property
    def interface_spec(self) -> InterfaceSpec[StorageHandle[R]]:
        return spec_of(StorageHandle[type(self.value)])


class TaskFunction[R: EntityBase](
    FxtrCallTarget,
    interface_id="TaskFunction^1",
    namespace_id="!builtin.tasks",
    stateless=True,
):
    """A function that can be run as a task by the hub.

    Task functions should implement the method run_task which will be called by the hub to run the
    task. The result should be an entity. Usually the task should return a specific output entity
    based on the definition of the task, although this is not currently tracked or enforced at the
    hub level.
    """

    @abc.abstractmethod
    async def run_task(self) -> R:
        """Run the task and return the result as an entity."""
        ...


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class TaskResultHandle[R: EntityBase](
    ComputedPlan[StorageHandle[R]],
    BuiltinEntityFromAnnotations,
    typespec_kind="task_run_result_handle",
):
    """Handle for the service that provides the result of a specific task run.

    This is a hard-coded implementation plan that indicates that the hub tooling should provide the
    result of a run of a specific task identified by a CID.

    At runtime, an implementation of this plan must return a constant, deterministic result, which
    must be the output of a specific run of the task identified by the task_ref. The exact result
    returned must be tracked in the provenance record for the implementation.

    Attributes:
        task_ref: The CID of the task whose result is provided by this provider.
        _handle_spec: The specification of the handle that provides the result.
    """

    task_ref: cbrrr.CID
    _handle_spec: InterfaceSpec[StorageHandle[R]]

    @property
    def interface_spec(self) -> InterfaceSpec[StorageHandle[R]]:
        return self._handle_spec


class TaskTagSequence(DataEntityBase, type_id="TaskTagSequence^1", namespace_id="!builtin"):
    """Python representation of a tag sequence for a task.

    A tag sequence is a sequence of tags that can be used to disambiguate tasks. Tag sequences are
    usually produced by calling `append_tag` on a Task object.

    Attributes:
        tags: The tags in the chain.
    """

    tags: tuple[AnyFrozenIPLDable, ...]


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class Task[R: EntityBase](BuiltinEntityFromAnnotations, typespec_kind="task"):
    """Python representation of a task.

    Task objects have a built-in singleton typespec, implicitly defined by BuiltinEntityBase.

    Attributes:
        function: The task function to execute.
        tag: An arbitrary tag for disambiguating samples of the same task. Tags can be used
            to create copies of tasks that have the exact same implementation but different runs,
            which makes it possible to refer to multiple runs of the same task function while still
            having a unique provenance mapping that associates each task ID with a single run.
    """

    function: Plan[TaskFunction[R]]
    tag: AnyFrozenIPLDable = None

    @functools.cached_property
    def result_handle(self) -> TaskResultHandle[R]:
        """Create a handle to the result of this task."""
        # TODO(daniel): this is a bit of a hack, it would be nicer if we didn't have to introspect
        # on the plan's bindings this way.
        fn_spec = self.function.interface_spec
        handle_spec = InterfaceSpec(
            defn=interface_defn_of(StorageHandle),
            bindings=freeze({"V": fn_spec.bindings["R"]}),
        )
        return TaskResultHandle(task_ref=self._fxtr_cid, _handle_spec=handle_spec)

    def extend_tag(self, new_tag: AnyFrozenIPLDable) -> Task[R]:
        """Extend the tag sequence of the task, so that it has distinct runs from other tags.

        Args:
            new_tag: The tag to append to the task. Can be any serializable object; often a string.
                If it is a TaskTagSequence, it will be concatenated to the existing tag sequence.

        Returns: A new Task with a tag that is a TaskTagSequence.
        """
        if self.tag is None:
            old_tag = TaskTagSequence(tags=())
        elif isinstance(self.tag, TaskTagSequence):
            old_tag = self.tag
        else:
            raise ValueError(
                "Can only use `append_tag` on a task with a TaskTagSequence or no tag; got"
                f" {self.tag=}"
            )
        if isinstance(new_tag, TaskTagSequence):
            return dataclasses.replace(
                self, tag=TaskTagSequence(tags=(*old_tag.tags, *new_tag.tags))
            )
        else:
            return dataclasses.replace(self, tag=TaskTagSequence(tags=(*old_tag.tags, new_tag)))


class WorkflowFunction[S: AnyFrozenIPLDable, R: EntityBase](
    FxtrCallTarget,
    interface_id="WorkflowFunction^1",
    namespace_id="!builtin.tasks",
    stateless=True,
    assumed_sendable_fwdrefs=("WorkflowState", "WorkflowResult"),
):
    """A function that can be run as a workflow by the hub.

    Workflows in fxtr consist of a chain of steps, where each step either produces a WorkflowResult
    (if it is done) or a WorkflowState (if it is not done).

    Workflow functions should implement the method run_step which will be called by the hub to
    run the first step of the workflow.
    """

    @abc.abstractmethod
    async def run_step(self) -> WorkflowState[S, R] | WorkflowResult[R]:
        """Run the (first/next) step of the workflow, producing a result or the next step to run.

        The result should be a WorkflowResult if this is the last step and the workflow is complete,
        or a WorkflowState if there are more steps that must be run in order to complete the
        workflow.
        """
        ...


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class Workflow[S: AnyFrozenIPLDable, R: EntityBase](
    BuiltinEntityFromAnnotations, typespec_kind="workflow"
):
    """Python representation of a workflow.

    Attributes:
        function: The workflow function to execute.
        tag: An arbitrary tag for disambiguating samples of the same workflow. Like for tasks, tags
            can be used to create copies of workflows that have the exact same implementation but
            different runs, which makes it possible to refer to multiple runs of the same workflow
            function while still having a unique provenance mapping that associates each workflow ID
            with a single run.
    """

    function: Plan[WorkflowFunction[S, R]]
    tag: AnyFrozenIPLDable = None

    @functools.cached_property
    def result_handle(self) -> WorkflowResultHandle[R]:
        """Create a handle to the result of this workflow."""
        # TODO(daniel): this is a bit of a hack, it would be nicer if we didn't have to introspect
        # on the plan's bindings this way.
        fn_spec = self.function.interface_spec
        handle_spec = InterfaceSpec(
            defn=interface_defn_of(StorageHandle),
            bindings=freeze({"V": fn_spec.bindings["R"]}),
        )
        return WorkflowResultHandle(workflow_ref=self._fxtr_cid, _handle_spec=handle_spec)

    @functools.cached_property
    def step_result_handle(self) -> WorkflowStepResultHandle[S, R]:
        """Create a handle to the result of the current step of this workflow."""
        fn_spec = self.function.interface_spec
        # TODO(daniel): this is a bit of a hack, it would be nicer if we didn't have to introspect
        # on the plan's bindings this way.
        handle_spec = _spec_of_with_mode(
            StorageHandle[WorkflowResult[R] | WorkflowState[S, R]],
            tv_mode="forbid",
            tv_substitutions={
                S: fn_spec.bindings["S"],
                R: fn_spec.bindings["R"],
            },
        )
        return WorkflowStepResultHandle(workflow_ref=self._fxtr_cid, _handle_spec=handle_spec)

    def extend_tag(self, new_tag: AnyFrozenIPLDable) -> Workflow[S, R]:
        """Extend the tag sequence of the workflow, so that it has distinct runs from other tags.

        Args:
            new_tag: The tag to append to the workflow. Can be any serializable object; often a
                string. If it is a TaskTagSequence, it will be concatenated to the existing tag
                sequence.

        Returns: A new Workflow with a tag that is a TaskTagSequence.
        """
        if self.tag is None:
            old_tag = TaskTagSequence(tags=())
        elif isinstance(self.tag, TaskTagSequence):
            old_tag = self.tag
        else:
            raise ValueError(
                "Can only use `append_tag` on a task with a TaskTagSequence or no tag; got"
                f" {self.tag=}"
            )
        if isinstance(new_tag, TaskTagSequence):
            return dataclasses.replace(
                self, tag=TaskTagSequence(tags=(*old_tag.tags, *new_tag.tags))
            )
        else:
            return dataclasses.replace(self, tag=TaskTagSequence(tags=(*old_tag.tags, new_tag)))


class WorkflowState[S: AnyFrozenIPLDable, R: EntityBase](
    DataEntityBase, type_id="WorkflowState^1", namespace_id="!builtin"
):
    """The state of a partially-completed workflow.

    Workflow functions produce WorkflowState to indicate that the workflow is not complete, and
    that more work needs to be done to complete the workflow. WorkflowState objects always include a
    new Workflow `next` that describes how to perform the remainder of the workflow starting from
    this state. They may also include arbitrary data in their `step_data` field, including possibly
    other tasks that can be run in parallel before running the `next` Workflow.
    """

    step_data: S
    next: Workflow[S, R]


class WorkflowResult[R: EntityBase](
    DataEntityBase, type_id="WorkflowResult^1", namespace_id="!builtin"
):
    """The result of a completed workflow."""

    result: R


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class WorkflowResultHandle[R: EntityBase](
    ComputedPlan[StorageHandle[R]],
    BuiltinEntityFromAnnotations,
    typespec_kind="workflow_run_result_handle",
):
    """Handle for the service that provides the result of a specific workflow run.

    This is a hard-coded implementation plan that indicates that the hub tooling should provide the
    result of a run of a specific workflow identified by a CID. Note that this specifically refers
    to the _final_ result of the workflow, which means it may involve traversing multiple steps of
    the workflow.

    Attributes:
        workflow_ref: The CID of the task whose result is provided by this provider.
        _handle_spec: The specification of the handle that provides the result.
    """

    workflow_ref: cbrrr.CID
    _handle_spec: InterfaceSpec[StorageHandle[R]]

    @property
    def interface_spec(self) -> InterfaceSpec[StorageHandle[R]]:
        return self._handle_spec


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class WorkflowStepResultHandle[S: AnyFrozenIPLDable, R: EntityBase](
    ComputedPlan[StorageHandle[WorkflowResult[R] | WorkflowState[S, R]]],
    BuiltinEntityFromAnnotations,
    typespec_kind="workflow_step_result_handle",
):
    """Handle for the service that provides the result of a run of a specific step of a workflow.

    This is a hard-coded implementation plan that indicates that the hub tooling should provide the
    result of a run of a specific step of a workflow identified by a CID. This always corresponds
    to either a WorkflowResult or a WorkflowState, depending on which was produced by the current
    step of the workflow.

    Attributes:
        workflow_ref: The CID of the workflow whose step result is provided by this provider.
        _handle_spec: The specification of the handle that provides the result.
    """

    workflow_ref: cbrrr.CID
    _handle_spec: InterfaceSpec[StorageHandle[WorkflowResult[R] | WorkflowState[S, R]]]

    @property
    def interface_spec(
        self,
    ) -> InterfaceSpec[StorageHandle[WorkflowResult[R] | WorkflowState[S, R]]]:
        return self._handle_spec


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class Run(BuiltinEntityFromAnnotations, typespec_kind="run"):
    """Python representation of a run.

    Runs identify both (1) the exact configuration of the task as well as (2) the result of the
    run.

    TODO: to enable refactoring, we may want to rebind this so that there is separately (a) a runlog
    that tracks _globally_ what tasks and runs were used, and (b) a rundata entry that purely
    identifies _locally_ what info a run used (e.g. the function, config, and dep _resolutions_
    without necessarily knowing about the task data).

    Attributes:
        task: The task to run.
        provenance: The provenance records for this run, indicating how all of the implementation
            plans in this task were resolved to implementations.
        run_data: The CID of the arbitrary entity that contains the result of the run. For a
            workflow, this is the result of the step, not necessarily the final result of the
            workflow.
        run_info: Metadata about the run execution itself. At the schema level this currently must
            be a BasicRunInfo entity; eventually we may recommend or mandate a different set of
            allowed entities for provenance tracking purposes.
    """

    task: CID_Of[Task | Workflow]
    provenance: CID_Of[ImplProvenanceSet]
    run_data: CID_Of[EntityBase]
    run_info: BasicRunInfo


class RunSet(DataEntityBase, type_id="RunSet^1", namespace_id="!builtin"):
    """Python representation of a run set entity.

    A run set is a set of runs, such that there is at most one run of any task in the runset, and
    if run R is in the runset and depends on previous run R', then run R' is also in the runset.

    Validity is not checked here, but only valid run sets should be published.

    Attributes:
        runs: A mapping from task CID to run CID.
    """

    runs: frozendict[CID_Of[Task | Workflow], CID_Of[Run]]


class RunSetRef(DataEntityBase, type_id="RunSetRef^1", namespace_id="!builtin"):
    """Python representation of a reference to a run set.

    Attributes:
        run_set: The CID of a run set.
    """

    run_set: CID_Of[RunSet]


class RunProvenance(DataEntityBase, namespace_id="!builtin.provenance", type_id="RunProvenance^1"):
    """Provenance for run data.

    Attributes:
        run_cid: The CID of the run data entity.
    """

    run_cid: CID_Of[Run]


class WorkflowResultProvenance(
    DataEntityBase, namespace_id="!builtin.provenance", type_id="WorkflowResultProvenance^1"
):
    """Provenance for workflow result data.

    Attributes:
        step_run_cids: The CIDs of the run data for each step of the workflow. The last run must
            produce the final WorkflowResult, and for each previous step, `step_run_cids[i]` must
            produce a WorkflowState such that `step_run_cids[i+1]` is a run of `step_run_cids[i]`'s
            "next_step" attribute.
    """

    step_run_cids: tuple[CID_Of[Run], ...]


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class PinnedResultHandle[R: EntityBase](
    ConfiguredPlan[StorageHandle[R]],
    BuiltinEntityFromAnnotations,
    typespec_kind="pinned_result_handle",
):
    """Handle for a service that provides a specific result from storage.

    This is a hard-coded implementation plan that indicates that the hub tooling should provide the
    result of a specific result identified by its provenance.

    Attributes:
        provenance: The provenance of the result to load.
        interface_spec: The specification of the handle that provides the result.
    """

    provenance: RunProvenance | WorkflowResultProvenance
    interface_spec: InterfaceSpec[StorageHandle[R]]


# endregion
####################################################################################################
# region: Runtime environment configuration
####################################################################################################


class RuntimeEnvRetriever[T: AnyFrozenIPLDable](
    FxtrCallTarget,
    interface_id="RuntimeEnvRetriever^1",
    namespace_id="!builtin",
    stateless=True,
):
    """Protocol for accessing runtime environment configuration.

    Runtime environment configuration includes any configuration that does NOT affect the end
    result of the task, and only affects how the task is executed. For instance, a user-specific
    rate limit, or a concurrency limit, or a level of log verbosity, or the path to a local
    filesystem directory that should be used for temporary files.

    Unlike most task configuration, which is part of the ID of the task, runtime environment
    configuration is NOT saved as part of the task. Instead, it is saved into the provenance record
    of each individual run of the task. This means that changing the runtime environment
    configuration does not invalidate previous runs of the task that used a different configuration.
    Runtime environment configuration can also be dynamically overridden when launching new runs of
    a task.

    Runtime environment configurations are generally declared using `declare_runtime_env_config`,
    and then (re)configured using the `configure` method.
    """

    @abc.abstractmethod
    async def get_runtime_config(self) -> T:
        """Get the runtime environment configuration.

        The result of calling this method MUST NOT be used in any way that would affect the end
        result of a successful task completion. It should only be used to configure the environment
        in which the task runs.
        """
        ...


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class RuntimeEnvConfigAssignment[T: AnyFrozenIPLDable](
    BuiltinEntityFromAnnotations, typespec_kind="runtime_env_assign"
):
    """A runtime environment configuration assignment.

    A runtime environment configuration assignment declares a specific value for a runtime
    environment configuration node.
    """

    handle: ExternalServiceIdentifier[RuntimeEnvRetriever[T]]
    value: T
    overrides: frozenset[RuntimeEnvConfigAssignment[T]] = frozenset()

    def configure(
        self,
        value: T,
        overriding: RuntimeEnvConfigAssignment[T] | Collection[RuntimeEnvConfigAssignment[T]] = (),
    ) -> RuntimeEnvConfigAssignment[T]:
        """Override an existing runtime environment configuration with a new value.

        When called, this method produces a new RuntimeEnvConfigAssignment that overrides this one
        with a new value. When this overridden assignment is discovered by fxtr (by being included
        in a top-level collection known to fxtr), it will be used to set the runtime environment
        configuration for tasks that depend on its plan.

        Note: right now, fxtr only allows running tasks declared in a single collection at a time,
        so only the assignments included in or referenced by that collection will be discovered.

        Args:
            value: The value to assign to the runtime environment configuration node.
            overriding: An optional assignment or collection of assignments to this same external
                config plan that should be overridden by the new value.

        Returns: A new RuntimeEnvConfigAssignment that overrides this one with the new value.
        """
        if isinstance(overriding, RuntimeEnvConfigAssignment):
            overriding = (overriding,)
        for o in overriding:
            if o.handle != self.handle:
                raise ValueError(
                    f"Overriding assignment has different handle: {o.handle} != {self.handle}"
                )
        return RuntimeEnvConfigAssignment(
            handle=self.handle, value=value, overrides=frozenset((self, *overriding))
        )


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class RuntimeEnvConfigDeclaration[T: AnyFrozenIPLDable](
    BuiltinEntityFromAnnotations, typespec_kind="runtime_env_decl"
):
    """An entity that declares a runtime environment configuration node, without assigning a value.

    This object is like a RuntimeEnvConfigAssignment except that it does not assign a value. Its
    main purpose is to simplify declaration of assignments to this plan when there are no default
    assignments.
    """

    handle: ExternalServiceIdentifier[RuntimeEnvRetriever[T]]

    def configure(
        self,
        value: T,
        overriding: RuntimeEnvConfigAssignment[T] | Collection[RuntimeEnvConfigAssignment[T]] = (),
    ) -> RuntimeEnvConfigAssignment[T]:
        if isinstance(overriding, RuntimeEnvConfigAssignment):
            overriding = (overriding,)
        for o in overriding:
            if o.handle != self.handle:
                raise ValueError(
                    f"Overriding assignment has different handle: {o.handle} != {self.handle}"
                )
        return RuntimeEnvConfigAssignment(
            handle=self.handle, value=value, overrides=frozenset(overriding)
        )


def declare_runtime_env_config[
    T: AnyFrozenIPLDable
](
    config_id: str,
    value: T | None = None,
    *,
    value_type: type[T] | None = None,
    namespace_id: str | None = None,
    doc: str | None = None,
) -> (RuntimeEnvConfigDeclaration[T] | RuntimeEnvConfigAssignment[T]):
    """Declare a runtime environment configuration node.

    Runtime environment configuration includes any configuration that does NOT affect the end
    result of the task, and only affects how the task is executed. For instance, a user-specific
    rate limit, or a concurrency limit, or a level of log verbosity, or the path to a local
    filesystem directory that should be used for temporary files.

    Unlike most task configuration, which is part of the ID of the task, runtime environment
    configuration is NOT saved as part of the task. Instead, it is saved into the provenance record
    of each individual run of the task. This means that changing the runtime environment
    configuration does not invalidate previous runs of the task that used a different configuration.
    Runtime environment configuration can also be dynamically overridden when launching new runs of
    a task.

    This function is used to declare a new runtime environment input for a task or plan. The ID of
    any dependent tasks or plans that use this input will refer only to the ID of this input node,
    NOT the value that was bound to it.

    Args:
        config_id: The ID of the runtime environment configuration node. This must be unique within
            the namespace.
        value: An optional default value to use for this input node. If provided, fxtr will
            automatically use this value when no other value is provided.
        value_type: The type of the value to use for this input node. Should be provided if the
            value is not provided.
        namespace_id: The namespace ID to associate this input node with.
        doc: Documentation about the value of this input node.

    Returns:
        A new runtime environment configuration declaration or assignment entity that declares this
        input node and possibly assigns a default value to it.
    """
    namespace_id = _get_type_namespace_id(namespace_id)

    if value_type is None:
        if value is None:
            raise ValueError("Either value_type or value must be provided")
        value_type = type(value)

    plan = ExternalServiceIdentifier(
        namespace_id=namespace_id,
        service_id=config_id,
        interface_spec=spec_of(RuntimeEnvRetriever[value_type]),
        _doc=doc,
    )

    if value is not None:
        return RuntimeEnvConfigAssignment(handle=plan, value=value, overrides=frozenset())
    else:
        return RuntimeEnvConfigDeclaration(handle=plan)


# was: class SecretsProtocol(FxtrCallTarget, ...)
class SecretRetriever[T: AnyFrozenIPLDable](
    FxtrCallTarget,
    interface_id="SecretRetriever^1",
    namespace_id="!builtin",
    stateless=True,
):
    """Protocol for accessing a secret.

    Secrets are values that are necessary for tasks to run, but should not be saved anywhere in the
    result of the task. For instance, an API key would be a secret.

    Similar to RuntimeEnvRetriever, secrets should usually NOT affect the end result of the task,
    but only affect how the task is executed. This is because the identity of the task does not
    depend on the value of the secret, and runs do not track the value of the secret.

    Secret configurations are generally declared using `declare_secret`, and then configured via
    the fxtr.toml file.
    """

    @abc.abstractmethod
    async def get_secret(self) -> T:
        """Get the secret."""
        ...


class SecretFromEnvVarDeclaration(
    DataEntityBase,
    type_id="SecretFromEnvVarDeclaration^1",
    namespace_id="!builtin",
):
    """An entity that declares that a secret should be retrieved from an environment variable."""

    handle: ExternalServiceIdentifier[SecretRetriever[str]]
    env_var: str


class SecretFromOptionalEnvVarDeclaration(
    DataEntityBase,
    type_id="SecretFromOptionalEnvVarDeclaration^1",
    namespace_id="!builtin",
):
    """An entity that declares that an optional secret should be retrieved from an env variable."""

    handle: ExternalServiceIdentifier[SecretRetriever[str | None]]
    env_var: str


@typing.overload
def declare_secret(
    secret_id: str,
    env_var: str,
    optional: Literal[True],
    *,
    namespace_id: str | None = None,
    doc: str | None = None,
) -> SecretFromOptionalEnvVarDeclaration: ...


@typing.overload
def declare_secret(
    secret_id: str,
    env_var: str,
    optional: Literal[False] = False,
    *,
    namespace_id: str | None = None,
    doc: str | None = None,
) -> SecretFromEnvVarDeclaration: ...


def declare_secret(
    secret_id: str,
    env_var: str,
    optional: bool = False,
    *,
    namespace_id: str | None = None,
    doc: str | None = None,
) -> SecretFromEnvVarDeclaration | SecretFromOptionalEnvVarDeclaration:
    """Declare a secret."""
    namespace_id = _get_type_namespace_id(namespace_id)
    if optional:
        plan = ExternalServiceIdentifier(
            namespace_id=namespace_id,
            service_id=secret_id,
            interface_spec=spec_of(SecretRetriever[str | None]),
            _doc=doc,
        )
        return SecretFromOptionalEnvVarDeclaration(handle=plan, env_var=env_var)
    else:
        plan = ExternalServiceIdentifier(
            namespace_id=namespace_id,
            service_id=secret_id,
            interface_spec=spec_of(SecretRetriever[str]),
            _doc=doc,
        )
        return SecretFromEnvVarDeclaration(handle=plan, env_var=env_var)


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class RuntimeCoordinatorBinding[I: FxtrCallTarget](
    BuiltinEntityFromAnnotations, typespec_kind="runtime_coord_binding"
):
    """A runtime environment coordinator binding.

    A runtime environment coordinator binding associates a specific implementation with a runtime
    coordinator declaration.
    """

    handle: ExternalServiceIdentifier[I]
    plan: Plan[I]
    overrides: frozenset[RuntimeCoordinatorBinding[I]] = frozenset()

    def configure(
        self,
        plan: Plan[I],
        overriding: RuntimeCoordinatorBinding[I] | Collection[RuntimeCoordinatorBinding[I]] = (),
    ) -> RuntimeCoordinatorBinding[I]:
        """Override an existing runtime coordinator binding with a new one.

        When called, this method produces a new RuntimeCoordinatorBinding that overrides this one
        with a new plan. When this overridden binding is discovered by fxtr (by being included
        in a top-level collection known to fxtr), it will be used to set the runtime coordinator
        binding for tasks that depend on its plan.

        Args:
            plan: The plan to assign to the runtime coordinator.
            overriding: An optional binding or collection of bindings to this same external
                coordinator plan that should be overridden by the new plan.

        Returns: A new RuntimeCoordinatorBinding that overrides this one with the new plan.
        """
        if isinstance(overriding, RuntimeCoordinatorBinding):
            overriding = (overriding,)
        for o in overriding:
            if o.handle != self.handle:
                raise ValueError(
                    f"Overriding binding has different handle: {o.handle} != {self.handle}"
                )
        return RuntimeCoordinatorBinding(
            handle=self.handle, plan=plan, overrides=frozenset((self, *overriding))
        )


@_register_builtin_entity_type_spec
@dataclasses.dataclass(frozen=True, eq=False)
class RuntimeCoordinatorDeclaration[I: FxtrCallTarget](
    BuiltinEntityFromAnnotations, typespec_kind="runtime_coord_decl"
):
    """An entity that declares a runtime environment coordinator node, without assigning an implementation.

    This object is like RuntimeCoordinatorBinding except that it does not assign a plan. Its
    main purpose is to simplify declaration of bindings to this plan when there are no default
    implementations.
    """

    handle: ExternalServiceIdentifier[I]

    def configure(
        self,
        plan: Plan[I],
        overriding: RuntimeCoordinatorBinding[I] | Collection[RuntimeCoordinatorBinding[I]] = (),
    ) -> RuntimeCoordinatorBinding[I]:
        if isinstance(overriding, RuntimeCoordinatorBinding):
            overriding = (overriding,)
        for o in overriding:
            if o.handle != self.handle:
                raise ValueError(
                    f"Overriding binding has different handle: {o.handle} != {self.handle}"
                )
        return RuntimeCoordinatorBinding(
            handle=self.handle, plan=plan, overrides=frozenset(overriding)
        )


def declare_runtime_coordinator[
    I: FxtrCallTarget
](
    coordinator_id: str,
    plan: Plan[I] | None = None,
    *,
    interface_spec: InterfaceSpec[I] | None = None,
    namespace_id: str | None = None,
    doc: str | None = None,
) -> (RuntimeCoordinatorDeclaration[I] | RuntimeCoordinatorBinding[I]):
    """Declare a runtime environment coordinator node.

    Runtime environment coordinator bindings let you associate an implementation (plan) with
    a runtime coordinator declaration. Coordinator bindings are like config assignments, but
    select specific coordinator logic for how tasks are actually managed and run.

    Unlike most task configuration, which is part of the ID of the task, runtime coordinator
    configuration is NOT saved as part of the task. Instead, it is saved into the provenance record
    of each individual run of the task. This means that changing the runtime coordinator
    implementation does not invalidate previous runs of the task that used a different
    implementation. Runtime coordinators can also be dynamically overridden when launching new runs
    of a task.

    For this to be valid, the specific choice of plan used as the runtime coordinator must NOT
    affect the results of the experiment in a meaningful way. Runtime coordinators should only be
    used to manage or coordinate work (e.g. for rate limiting purposes) or to allow tasks to report
    state that does not affect the result of the task (e.g. to send state updates). Because of
    this restriction, runtime coordinator plans _are_ allowed to implement mutable interfaces,
    whereas most plans cannot. Additionally, runtime coordinator plans are guaranteed to resolve to
    the same implementation within a single fxtr execution server (so, in particular, any
    concurent tasks launched at the same time will always bind to the same coordinator object).

    To ensure that runtime coordinators do not affect reproducibility of the results, coordinators
    should NOT be used to exchange information between tasks that would affect their outputs. An
    individual task should not be able to tell whether or not other tasks are running, and it should
    always be valid to shut down and restart the coordinator between tasks.

    This function is used to declare a new runtime environment coordinator input for a task or plan.
    The ID of any dependent tasks or plans that use this input will refer only to the ID of this
    input node, NOT the plan that was bound to it.

    Args:
        coordinator_id: The ID of the runtime coordinator node. This must be unique within the
            namespace.
        plan: An optional default plan to use for this input node. If provided, fxtr will
            use this plan as the coordinator binding unless overridden.
        plan_type: The type of the plan to use for this input node. Should be provided if plan is not.
        namespace_id: The namespace ID to associate this input node with.
        doc: Documentation about the value of this input node.

    Returns:
        A new runtime coordinator declaration or binding entity that declares this
        input node and possibly assigns a default plan to it.
    """
    namespace_id = _get_type_namespace_id(namespace_id)

    if interface_spec is None:
        if plan is None:
            raise ValueError("Either interface_spec or plan must be provided")
        interface_spec = plan.interface_spec

    handle = ExternalServiceIdentifier(
        namespace_id=namespace_id,
        service_id=coordinator_id,
        interface_spec=interface_spec,
        _doc=doc,
    )

    if plan is not None:
        return RuntimeCoordinatorBinding(handle=handle, plan=plan, overrides=frozenset())
    else:
        return RuntimeCoordinatorDeclaration(handle=handle)


# endregion
####################################################################################################
# region: Access paths
####################################################################################################


# Note: Access paths are hardcoded into the system and are awkward to encode as entities, so we
# instead manipulate them as dataclasses and save them when needed as JSON blobs. This should be
# fine because we will always know that these are supposed to be access paths from context due to
# where they are used.

# TODO: consider whether it would make more sense to make these entities again, to avoid the
# awkwardness of JSON -> AccessPath -> JSON.


@dataclasses.dataclass(frozen=True)
class AccessPathStep_GetAttr:
    """Accessing an attribute of a value."""

    tag: ClassVar[str] = "attr"

    attr_name: str

    def to_ipld(self) -> AnyIPLDable:
        return {"tag": self.tag, "attr_name": self.attr_name}

    @classmethod
    def from_ipld(cls, ipld_value: AnyIPLDable) -> Self:
        assert isinstance(ipld_value, frozendict)
        attr_name = ipld_value["attr_name"]
        assert isinstance(attr_name, str)
        return cls(attr_name=attr_name)


@dataclasses.dataclass(frozen=True)
class AccessPathStep_GetItem_Int:
    """Accessing an item in a value by integer key."""

    tag: ClassVar[str] = "item_int"

    key: int

    def to_ipld(self) -> AnyIPLDable:
        return {"tag": self.tag, "key": self.key}

    @classmethod
    def from_ipld(cls, ipld_value: AnyIPLDable) -> Self:
        assert isinstance(ipld_value, frozendict)
        key = ipld_value["key"]
        assert isinstance(key, int)
        return cls(key=key)


@dataclasses.dataclass(frozen=True)
class AccessPathStep_GetItem_Str:
    """Accessing an item in a value by string key."""

    tag: ClassVar[str] = "item_str"

    key: str

    def to_ipld(self) -> AnyIPLDable:
        return {"tag": self.tag, "key": self.key}

    @classmethod
    def from_ipld(cls, ipld_value: AnyIPLDable) -> Self:
        assert isinstance(ipld_value, frozendict)
        key = ipld_value["key"]
        assert isinstance(key, str)
        return cls(key=key)


@dataclasses.dataclass(frozen=True)
class AccessPathStep_GetItem_Entity:
    """Accessing an item in a value by entity key."""

    tag: ClassVar[str] = "item_entity"
    key: EntityBase

    def to_ipld(self) -> AnyIPLDable:
        return {"tag": self.tag, "key": self.key}

    @classmethod
    def from_ipld(cls, ipld_value: AnyIPLDable) -> Self:
        assert isinstance(ipld_value, frozendict)
        key = ipld_value["key"]
        assert isinstance(key, EntityBase)
        return cls(key=key)


@dataclasses.dataclass(frozen=True)
class AccessPathStep_GetItem_CID:
    """Accessing an item in a value by CID key."""

    tag: ClassVar[str] = "item_cid"
    key: cbrrr.CID

    def to_ipld(self) -> AnyIPLDable:
        return {"tag": self.tag, "key_base32": self.key.encode(base="base32")}

    @classmethod
    def from_ipld(cls, ipld_value: AnyIPLDable) -> Self:
        assert isinstance(ipld_value, frozendict)
        key_base32 = ipld_value["key_base32"]
        assert isinstance(key_base32, str)
        return cls(key=cbrrr.CID.decode(key_base32))


@dataclasses.dataclass(frozen=True)
class AccessPathStep_FrozenDictSortedKey:
    """Accessing a key in a frozendict by its sorted position (sorted by CID)."""

    tag: ClassVar[str] = "frozen_dict_sorted_key"
    sorted_key_index: int

    def to_ipld(self) -> AnyIPLDable:
        return {"tag": self.tag, "sorted_key_index": self.sorted_key_index}

    @classmethod
    def from_ipld(cls, ipld_value: AnyIPLDable) -> Self:
        assert isinstance(ipld_value, frozendict)
        sorted_key_index = ipld_value["sorted_key_index"]
        assert isinstance(sorted_key_index, int)
        return cls(sorted_key_index=sorted_key_index)


@dataclasses.dataclass(frozen=True)
class AccessPathStep_SetElementSortedOrder:
    """Accessing an element in a set by its sorted position (sorted by DAG-CBOR codec bytes, unhashed)."""

    tag: ClassVar[str] = "set_element_sorted_order"
    sorted_element_index: int

    def to_ipld(self) -> AnyIPLDable:
        return {
            "tag": self.tag,
            "sorted_element_index": self.sorted_element_index,
        }

    @classmethod
    def from_ipld(cls, ipld_value: AnyIPLDable) -> Self:
        assert isinstance(ipld_value, frozendict)
        sorted_element_index = ipld_value["sorted_element_index"]
        assert isinstance(sorted_element_index, int)
        return cls(sorted_element_index=sorted_element_index)


@dataclasses.dataclass(frozen=True)
class AccessPathStep_EntityTypespec:
    """Special access: accessing the typespec of an entity.

    Roughly equivalent to AccessPathStep_GetAttr(attr_name="_fxtr_typespec"), but _only_ used when
    accessing the typespec of an entity; useful for simplifying the displayed representation of the
    access path for an entity type definition, and also hides implementation details of the
    typespec attribute, which may change later.
    """

    tag: ClassVar[str] = "entity_typespec"

    def to_ipld(self) -> AnyIPLDable:
        return {"tag": self.tag}

    @classmethod
    def from_ipld(cls, ipld_value: AnyIPLDable) -> Self:
        return cls()


@dataclasses.dataclass(frozen=True)
class AccessPathStep_UserDefinedClass_ValueTypespec:
    """Special access: accessing the value_typespec of a user-defined class.

    Roughly equivalent to AccessPathStep_GetAttr(attr_name="value_typespec"), but _only_ used when
    accessing the value_typespec of a data entity or service identifier class; useful for
    simplifying the displayed representation of the access path for a type definition.
    """

    tag: ClassVar[str] = "data_entity_class_typespec"

    def to_ipld(self) -> AnyIPLDable:
        return {"tag": self.tag}

    @classmethod
    def from_ipld(cls, ipld_value: AnyIPLDable) -> Self:
        return cls()


@dataclasses.dataclass(frozen=True)
class AccessPathStep_InterfaceDefn:
    """Special access: accessing the InterfaceDefn from an abstract call target class.

    Represents calling core.interface_defn_of(call_target_class), but represented as an access
    path step for tracking purposes in collections. Used to record the relationship between an
    abstract call target class and its InterfaceDefn entity.
    """

    tag: ClassVar[str] = "interface_defn"

    def to_ipld(self) -> AnyIPLDable:
        return {"tag": self.tag}

    @classmethod
    def from_ipld(cls, ipld_value: AnyIPLDable) -> Self:
        return cls()


AccessPathStep = (
    AccessPathStep_GetAttr
    | AccessPathStep_GetItem_Int
    | AccessPathStep_GetItem_Str
    | AccessPathStep_GetItem_Entity
    | AccessPathStep_GetItem_CID
    | AccessPathStep_FrozenDictSortedKey
    | AccessPathStep_SetElementSortedOrder
    | AccessPathStep_EntityTypespec
    | AccessPathStep_UserDefinedClass_ValueTypespec
    | AccessPathStep_InterfaceDefn
)
AccessPath = tuple[AccessPathStep, ...]


def frozen_ipldable_to_access_path_step(ipld_value: AnyFrozenIPLDable) -> AccessPathStep:
    """Converts an IPLD value back to an access path."""
    assert isinstance(ipld_value, frozendict), f"Expected frozendict, got {type(ipld_value)}"
    ipld_value["tag"]
    for ty in AccessPathStep.__args__:
        if ty.tag == ipld_value["tag"]:
            return ty.from_ipld(ipld_value)
    raise ValueError(f"Unknown IPLD access path step: {ipld_value}")


def frozen_ipldable_to_access_path(ipld_value: AnyFrozenIPLDable) -> AccessPath:
    """Converts an IPLD value back to an access path."""
    assert isinstance(ipld_value, tuple), f"Expected tuple, got {type(ipld_value)}"
    return tuple(frozen_ipldable_to_access_path_step(step) for step in ipld_value)


def access_path_to_str(root_str: str, access_path: AccessPath) -> str:
    """Converts an access path to a string."""
    parts = [root_str]
    for step in access_path:
        match step:
            case AccessPathStep_GetAttr(attr_name=attr_name):
                parts.append(f".{attr_name}")
            case (
                AccessPathStep_GetItem_Str(key=key)
                | AccessPathStep_GetItem_Int(key=key)
                | AccessPathStep_GetItem_CID(key=key)
            ):
                parts.append(f"[{repr(key)}]")
            case AccessPathStep_GetItem_Entity(key=key):
                parts.append(f"[lookup_entity({repr(entity_cid(key))})]")
            case AccessPathStep_FrozenDictSortedKey(sorted_key_index=sorted_key_index):
                parts.insert(0, "sorted(")
                parts.append(f".keys())[{sorted_key_index}]")
            case AccessPathStep_SetElementSortedOrder(sorted_element_index=sorted_element_index):
                parts.insert(0, "sorted(")
                parts.append(f")[{sorted_element_index}]")
            case AccessPathStep_EntityTypespec():
                parts.insert(0, "typespec(")
                parts.append(")")
            case AccessPathStep_UserDefinedClass_ValueTypespec():
                parts.append(".value_typespec")
            case AccessPathStep_InterfaceDefn():
                parts.insert(0, "interface_defn_of(")
                parts.append(")")
            case _:
                raise ValueError(f"Unknown access path step: {step}")
    result = "".join(parts)
    return result


# endregion
####################################################################################################
