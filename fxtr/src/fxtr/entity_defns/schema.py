"""Schemas for regular data: the value type system shared by arrays, steps, and workflows.

A :data:`Schema` describes data of a fixed, *regular* shape: leaves (``int``, ``float``, ``bool``,
``str``, ``bytes``), ids (an entity id, optionally annotated with the type id the entity is
expected to have), lists, structs with fixed (unordered) field names, and nullable wrappers
(never directly nested). Because the shape is fixed, values can be stored, queried, and combined
predictably.

Schemas are written in Python: as **type annotations**, read by :func:`schema_from_annotation`
(``int``, ``Sequence[str]``, ``BoundID[Bio]``, ``X | None``, a ``TypedDict`` class, ...), or with
the **constructors** :func:`id_of`, :func:`list_of`, :func:`struct_of`, :func:`optional` for what
no annotation expresses (an id of a type without a class, an anonymous struct). Every API
taking a schema accepts a :data:`SchemaSpec`: a :data:`Schema` object or an annotation, normalized
by :func:`as_schema`. There is deliberately no text form: ``str(schema)`` is for display only.

**Values** are checked against a schema strictly (no coercion: a ``bool`` is not an ``int``, an
``int`` is not a ``float``) and frozen by :func:`coerce_value`, lists as tuples and structs as
frozendicts with fields sorted by name, so values are hashable. :func:`infer_schema` infers a
schema from a value as a convenience.

**Ids.** An :class:`IdSchema` holds an entity id. It may name the *type* of the entity the id
points to (a type id string, as used for ``$type`` tags): ``id_of(Bio)`` or ``BoundID[Bio]`` for
an entity class, ``id_of("org.example.Bio")`` for a bare type id. The type is an annotation:
checking a value never loads the referenced entity.

**Presentation.** A schema also says what Python form values take when an array hands them out
(:func:`present_value`): an :class:`IdSchema` may carry a *loader*, so its ids present as
:class:`~fxtr.core.entities.BoundID`\\ s; a :class:`StructSchema` may carry its TypedDict class, so
its values present as dicts; a :class:`ListSchema` may present as ``list`` rather than ``tuple``.
Presentation is off the wire and takes no part in equality or hashing: it is how *this program*
declared the data, not a property of the data. :func:`schema_from_annotation` sets it from the
annotation (``BoundID[T]``, a TypedDict, ``list[X]``), and "check, then adopt the declared type"
(:meth:`~fxtr.entity_defns.array.Array.retype`) carries it to values loaded from elsewhere.

**Compatibility.** :func:`is_assignable` says whether a value of one schema can be used where
another is expected: schemas must match exactly except that a non-nullable schema is assignable to
its nullable counterpart, and a typed id to the untyped ``id``, recursively through lists and
struct fields.

**Wire form.** A schema serializes (:func:`schema_to_wire`) as a leaf name string (``"id"`` for
an untyped id), or a single-key map ``{"list": S}``, ``{"struct": {name: S}}``,
``{"nullable": S}``, or ``{"id": "<type id>"}``; a frozen value thaws (:func:`value_to_wire`) to
plain JSON-shaped data (lists and mappings).
"""

from __future__ import annotations

import collections.abc
import dataclasses
import types
import typing
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypeGuard, get_args

from frozendict import frozendict

from fxtr.core.entities import BoundID, Entity, EntityLoader
from fxtr.core.entity_data import (
    EntityID,
    FxtrDataValue,
    StorableFxtrDataValue,
)
from fxtr.core.loader_registry import DEFAULT_ENTITY_LOADER_REGISTRY, EntityLoaderRegistry


class SchemaError(ValueError):
    """A schema is malformed."""


class ValueSchemaError(ValueError):
    """A value does not conform to a schema, or cannot be given one."""


# ---- schemas ----

type LeafKind = Literal["int", "float", "bool", "str", "bytes"]
"""The leaf schemas (entity ids are :class:`IdSchema`, not leaves)."""

LEAF_KINDS: frozenset[str] = frozenset(get_args(LeafKind.__value__))


@dataclasses.dataclass(frozen=True)
class LeafSchema:
    """A leaf schema (see :data:`LeafKind`)."""

    kind: LeafKind

    def __post_init__(self) -> None:
        if self.kind not in LEAF_KINDS:
            raise SchemaError(
                f"Unknown leaf schema {self.kind!r}; expected one of {sorted(LEAF_KINDS)}"
            )

    def __str__(self) -> str:
        return self.kind


@dataclasses.dataclass(frozen=True)
class IdSchema:
    """An id schema: an entity id, optionally annotated with the entity's type id.

    ``type_id`` is a type id string (what the referenced entity's ``$type`` tag is expected to
    hold), or ``None`` for an untyped id. It is an annotation only: :func:`coerce_value` does not
    load the referenced entity to check its type. ``str`` renders ``id`` or ``id[<type id>]``.

    ``loader`` is *presentation*: the :class:`~fxtr.core.entities.EntityLoader` values of this
    schema are handed out bound to (see the module docstring). It takes no part in equality,
    hashing, or the wire form. :func:`id_of` builds a typed id with its loader from an entity
    class.
    """

    type_id: str | None = None
    loader: EntityLoader[Any] | None = dataclasses.field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.type_id is not None and not (isinstance(self.type_id, str) and self.type_id):
            raise SchemaError(f"Ref type must be a non-empty string or None, got {self.type_id!r}")
        if self.loader is not None and not isinstance(self.loader, EntityLoader):
            raise SchemaError(f"Id loader must be an EntityLoader or None, got {self.loader!r}")

    def __str__(self) -> str:
        return "id" if self.type_id is None else f"id[{self.type_id}]"


@dataclasses.dataclass(frozen=True)
class ListSchema:
    """A list schema: a variable-length sequence of ``element`` values.

    ``present_as`` is presentation (see the module docstring): whether values of this schema are
    handed out as tuples (the frozen form, the default) or as fresh ``list`` copies, as a
    ``list[X]`` annotation asks. It takes no part in equality, hashing, or the wire form.
    """

    element: Schema
    present_as: type[tuple[Any, ...]] | type[list[Any]] = dataclasses.field(
        default=tuple, compare=False
    )

    def __post_init__(self) -> None:
        if self.present_as not in (tuple, list):
            raise SchemaError(f"List presentation must be tuple or list, got {self.present_as!r}")

    def __str__(self) -> str:
        return f"list[{self.element}]"


@dataclasses.dataclass(frozen=True)
class StructSchema:
    """A struct schema: a mapping with exactly the given field names, each with its own schema.

    Field order is not significant.

    ``python_type`` is presentation (see the module docstring): a ``TypedDict`` class whose keys
    are exactly the fields, meaning values of this schema are handed out as fresh ``dict``\\ s of
    that shape rather than as frozendicts. It takes no part in equality, hashing, or the wire
    form. :func:`schema_from_annotation` sets it for a TypedDict annotation.
    """

    fields: frozendict[str, Schema]
    python_type: type | None = dataclasses.field(default=None, compare=False)

    def __post_init__(self) -> None:
        # Accept any mapping at construction, store a frozendict so the schema stays hashable.
        object.__setattr__(self, "fields", frozendict(self.fields))
        for name, schema in self.fields.items():
            if not isinstance(name, str) or not name:
                raise SchemaError(f"Struct field names must be non-empty strings, got {name!r}")
            if not is_schema(schema):
                raise SchemaError(f"Field {name!r}: expected a Schema, got {schema!r}")
        if self.python_type is not None:
            if not _is_typeddict(self.python_type):
                raise SchemaError(
                    f"Struct presentation must be a TypedDict class, got {self.python_type!r}"
                )
            keys = set(self.python_type.__required_keys__)  # type: ignore[attr-defined]
            if keys != set(self.fields):
                raise SchemaError(
                    f"TypedDict {self.python_type.__name__} has keys {sorted(keys)}, but the"
                    f" struct has fields {sorted(self.fields)}"
                )

    @property
    def names(self) -> tuple[str, ...]:
        """The field names, sorted."""
        return tuple(sorted(self.fields))

    def __str__(self) -> str:
        inner = ", ".join(f"{name}: {self.fields[name]}" for name in self.names)
        return f"struct{{{inner}}}"


@dataclasses.dataclass(frozen=True)
class NullableSchema:
    """A nullable schema: ``None`` or an ``inner`` value (which may not itself be nullable)."""

    inner: Schema

    def __post_init__(self) -> None:
        if isinstance(self.inner, NullableSchema):
            raise SchemaError(f"Nullable schemas cannot be nested: {self.inner}?")

    def __str__(self) -> str:
        return f"{self.inner}?"


type Schema = LeafSchema | IdSchema | ListSchema | StructSchema | NullableSchema
"""A schema. Nesting is regular, so values can be stored and queried in a fixed shape."""

_SCHEMA_CLASSES = (LeafSchema, IdSchema, ListSchema, StructSchema, NullableSchema)


def is_schema(obj: object) -> TypeGuard[Schema]:
    """Whether ``obj`` is a :data:`Schema` instance."""
    return isinstance(obj, _SCHEMA_CLASSES)


def contains_entity_ids(schema: Schema) -> bool:
    """Whether values described by this schema can contain entity references."""
    match schema:
        case IdSchema():
            return True
        case NullableSchema(inner) | ListSchema(inner):
            return contains_entity_ids(inner)
        case StructSchema(fields):
            return any(contains_entity_ids(field) for field in fields.values())
        case _:
            return False


type SchemaSpec = Schema | Any
"""A schema, or a Python type annotation that determines one (see :func:`schema_from_annotation`).
Normalized by :func:`as_schema`."""


def as_schema(spec: SchemaSpec) -> Schema:
    """Normalize a :data:`SchemaSpec` to a :data:`Schema`: a schema is returned as is, anything
    else is read as a type annotation (:func:`schema_from_annotation`).

    Raises:
        SchemaError: If the annotation does not determine a schema (a string in particular: there
            is no text form of schemas).
    """
    if is_schema(spec):
        return spec
    if isinstance(spec, str):
        raise SchemaError(
            f"A schema is not written as a string ({spec!r}); use a type annotation (int,"
            " Sequence[str], BoundID[T], a TypedDict, ...) or the constructors id_of / list_of /"
            " struct_of / optional"
        )
    return schema_from_annotation(spec)


# ---- building schemas from Python ----


def id_of(
    target: type[Entity] | str | None = None,
    *,
    registry: EntityLoaderRegistry | None = None,
) -> IdSchema:
    """An id schema for references to ``target``.

    * An entity class: the id is typed with the class's
      :meth:`~fxtr.core.entities.Entity.fxtr_type_id` (or, failing that, the loader's
      :attr:`~fxtr.core.entities.EntityLoader.static_type_id`), and carries the loader the
      registry resolves for the class, so values present as
      :class:`~fxtr.core.entities.BoundID`\\ s of it. A class with no static type id
      (:class:`~fxtr.entity_defns.unknown_entity.UnknownEntity`) gives an untyped id that still
      carries the loader.
    * A string: a type id; typed, with no loader.
    * ``None``: an untyped id.

    Args:
        target: What the ids refer to.
        registry: The loader registry to resolve an entity class against; the shared default if
            omitted.

    Raises:
        SchemaError: If ``target`` is a class the registry cannot build a loader for (an abstract
            entity class without a declared type, say) or is not an entity class at all.
    """
    if target is None:
        return IdSchema()
    if isinstance(target, str):
        return IdSchema(target)
    if isinstance(target, type) and issubclass(target, Entity):
        try:
            loader = (registry or DEFAULT_ENTITY_LOADER_REGISTRY).loader_for(target)
        except (LookupError, TypeError, ValueError) as exc:
            raise SchemaError(f"No loader for entity class {target.__name__}: {exc}") from exc
        type_id = target.fxtr_type_id()
        if type_id is None:
            type_id = loader.static_type_id
        return IdSchema(type_id, loader)
    raise SchemaError(
        f"id_of expects an entity class, a type id, or None, got {target!r}; a reference to an"
        " inline tagged value or a non-entity class is not a schema"
    )


def list_of(element: SchemaSpec) -> ListSchema:
    """A list schema of ``element`` values (presented as tuples)."""
    return ListSchema(as_schema(element))


def struct_of(**fields: SchemaSpec) -> StructSchema:
    """A struct schema with the given fields, e.g. ``struct_of(score=float, note=optional(str))``
    (presented as frozendicts; declare a TypedDict instead to present as dicts)."""
    return StructSchema(frozendict({name: as_schema(spec) for name, spec in fields.items()}))


def optional(inner: SchemaSpec) -> NullableSchema:
    """The nullable form of ``inner``, which may not itself be nullable.

    Raises:
        SchemaError: If ``inner`` is already nullable.
    """
    return NullableSchema(as_schema(inner))


_LEAF_SCHEMAS_BY_TYPE: dict[type, LeafSchema] = {
    int: LeafSchema("int"),
    float: LeafSchema("float"),
    bool: LeafSchema("bool"),
    str: LeafSchema("str"),
    bytes: LeafSchema("bytes"),
}

_ACCEPTED_ANNOTATIONS = (
    "int, float, bool, str, bytes; Sequence[X], tuple[X, ...], list[X]; X | None; EntityID;"
    " BoundID[T] for an entity class T; a total TypedDict; or a Schema object"
)


def _is_typeddict(obj: object) -> bool:
    """Whether ``obj`` is a TypedDict class from :mod:`typing` or :mod:`typing_extensions`."""
    return (
        isinstance(obj, type)
        and issubclass(obj, dict)
        and hasattr(obj, "__total__")
        and hasattr(obj, "__required_keys__")
    )


def _has_extra_items(cls: type) -> bool:
    """Whether a TypedDict class declares ``extra_items=`` (PEP 728; ``typing_extensions``)."""
    extra = getattr(cls, "__extra_items__", None)
    if extra is None:
        return False
    try:
        import typing_extensions
    except ImportError:  # pragma: no cover - typing_extensions is present in practice
        return True
    return extra is not getattr(typing_extensions, "NoExtraItems", object())


def schema_from_annotation(
    annotation: Any, *, registry: EntityLoaderRegistry | None = None
) -> Schema:
    """The schema a Python type annotation determines.

    The accepted forms are exactly those whose *presented* value has the annotated type (see the
    module docstring), so an annotation never lies about what it receives:

    ============================================  ==================================================
    annotation                                    schema
    ============================================  ==================================================
    ``int``, ``float``, ``bool``, ``str``, ``bytes``  the leaf
    ``Sequence[X]``, ``tuple[X, ...]``            ``list_of(X)``, presented as a tuple
    ``list[X]``                                   ``list_of(X)``, presented as a list
    ``X | None``                                  ``optional(X)`` (``X`` not itself optional)
    ``EntityID``                                  ``id_of()``
    ``BoundID[T]``, ``T`` an entity class         ``id_of(T)`` (typed when ``T`` knows its type)
    a total ``TypedDict`` class                   a struct of its fields, presented as that class
    ``Annotated[X, ...]``                         the schema of ``X`` (metadata is not read here)
    a :data:`Schema` object                       itself
    ============================================  ==================================================

    A TypedDict may come from :mod:`typing` or :mod:`typing_extensions`; it must be total with
    every key required, and may not declare ``closed=False`` or ``extra_items`` (PEP 728); every
    TypedDict is treated as closed. Everything else is an error, including ``dict`` / ``Mapping``
    (no fixed fields: declare a TypedDict or use :func:`struct_of`), ``Any``, bare entity classes
    (write ``BoundID[T]``), unions other than ``X | None``, and unresolved forward references.

    Args:
        annotation: The annotation, already resolved (as :func:`typing.get_type_hints` returns).
        registry: The loader registry entity classes are resolved against; the shared default if
            omitted.

    Raises:
        SchemaError: If the annotation does not determine a schema.
    """
    return _from_annotation(annotation, "annotation", registry)


def expand_type_alias(annotation: Any) -> Any:
    """Expand a PEP 695 ``type`` alias to the type it stands for, substituting its parameters.

    ``type Foo[T] = Bar[T]`` used as ``Foo[int]`` becomes ``Bar[int]``, and a non-generic
    ``type Foo = Bar`` becomes ``Bar``; an alias of an alias is expanded repeatedly. Anything that
    is not a :class:`~typing.TypeAliasType` (bare or subscripted) is returned unchanged. This lets a
    reader treat ``ScalarHandle[X]`` exactly like the ``Annotated[ArrayHandle[X], "[]"]`` it names.
    """
    while True:
        if isinstance(annotation, typing.TypeAliasType):
            annotation = annotation.__value__
            continue
        origin = typing.get_origin(annotation)
        if isinstance(origin, typing.TypeAliasType):
            args = get_args(annotation)
            value: Any = origin.__value__
            annotation = value[args[0]] if len(args) == 1 else value[args]
            continue
        return annotation


def _from_annotation(ann: Any, where: str, registry: EntityLoaderRegistry | None) -> Schema:
    ann = expand_type_alias(ann)
    if is_schema(ann):
        return ann
    if isinstance(ann, str):
        raise SchemaError(
            f"{where}: unresolved forward reference {ann!r}; resolve annotations (e.g. with"
            " typing.get_type_hints) before reading schemas off them"
        )
    origin = typing.get_origin(ann)
    args = get_args(ann)
    if origin is Annotated:
        return _from_annotation(args[0], where, registry)
    if origin in (typing.Union, types.UnionType):
        members = [m for m in args if m is not type(None)]
        if len(args) == 2 and len(members) == 1:
            inner = _from_annotation(members[0], where, registry)
            if isinstance(inner, NullableSchema):
                raise SchemaError(f"{where}: {ann!r} is optional twice over")
            return NullableSchema(inner)
        raise SchemaError(f"{where}: a union is not a schema (only `X | None` is), got {ann!r}")
    if ann is type(None):
        raise SchemaError(f"{where}: None alone is not a schema; write `X | None`")
    if isinstance(ann, type) and ann in _LEAF_SCHEMAS_BY_TYPE:
        return _LEAF_SCHEMAS_BY_TYPE[ann]
    if ann is EntityID:
        return IdSchema()
    if ann is BoundID or origin is BoundID:
        if len(args) != 1 or not (isinstance(args[0], type) and issubclass(args[0], Entity)):
            raise SchemaError(
                f"{where}: BoundID needs an entity class as its target (e.g. BoundID[MyEntity]),"
                f" got {ann!r}"
            )
        try:
            return id_of(args[0], registry=registry)
        except SchemaError as exc:
            raise SchemaError(f"{where}: {exc}") from exc
    if origin is list:
        return ListSchema(_from_annotation(args[0], f"{where}[]", registry), present_as=list)
    if origin is tuple:
        if len(args) != 2 or args[1] is not Ellipsis:
            raise SchemaError(
                f"{where}: only the variable-length form tuple[X, ...] is a schema, got {ann!r}"
            )
        return ListSchema(_from_annotation(args[0], f"{where}[]", registry))
    if origin is collections.abc.Sequence:
        return ListSchema(_from_annotation(args[0], f"{where}[]", registry))
    if _is_typeddict(ann):
        return _struct_from_typeddict(ann, where, registry)
    # Near misses, each with a pointer to what to write instead.
    if isinstance(ann, type) and issubclass(ann, Entity):
        raise SchemaError(
            f"{where}: an entity class is not a schema; write BoundID[{ann.__name__}] for a"
            " reference to one"
        )
    if ann in (dict, collections.abc.Mapping) or origin in (dict, collections.abc.Mapping):
        raise SchemaError(
            f"{where}: {ann!r} has no fixed fields; declare a TypedDict, or use struct_of(...)"
        )
    if ann is Any or ann is object:
        raise SchemaError(f"{where}: {ann!r} is not a schema; accepted are {_ACCEPTED_ANNOTATIONS}")
    raise SchemaError(f"{where}: {ann!r} is not a schema; accepted are {_ACCEPTED_ANNOTATIONS}")


def _struct_from_typeddict(
    cls: type, where: str, registry: EntityLoaderRegistry | None
) -> StructSchema:
    hints = typing.get_type_hints(cls, include_extras=True)
    required = set(cls.__required_keys__)  # type: ignore[attr-defined]
    if not cls.__total__ or required != set(hints):  # type: ignore[attr-defined]
        raise SchemaError(
            f"{where}: TypedDict {cls.__name__} must be total with every key required (no"
            " total=False, no NotRequired), since a struct has exactly its fields"
        )
    if getattr(cls, "__closed__", None) is False or _has_extra_items(cls):
        raise SchemaError(
            f"{where}: TypedDict {cls.__name__} declares closed=False or extra_items, but a"
            " struct has exactly its fields"
        )
    fields = {
        name: _from_annotation(hint, f"{where}.{name}", registry) for name, hint in hints.items()
    }
    return StructSchema(frozendict(fields), python_type=cls)


def schema_to_wire(schema: Schema) -> StorableFxtrDataValue:
    """Serialize a schema (see the module docstring for the wire form)."""
    match schema:
        case LeafSchema(kind):
            return kind
        case IdSchema(None):
            return "id"
        case IdSchema(type_id):
            return {"id": type_id}
        case ListSchema(element):
            return {"list": schema_to_wire(element)}
        case StructSchema(fields):
            return {"struct": {name: schema_to_wire(s) for name, s in fields.items()}}
        case NullableSchema(inner):
            return {"nullable": schema_to_wire(inner)}


def schema_from_wire(data: object) -> Schema:
    """Deserialize a schema (strictly) from its wire form.

    Raises:
        SchemaError: If the data is not a valid schema.
    """
    if isinstance(data, str):
        if data == "id":
            return IdSchema()
        if data not in LEAF_KINDS:
            raise SchemaError(f"Unknown leaf schema {data!r}")
        return LeafSchema(data)  # type: ignore[arg-type]
    if isinstance(data, Mapping) and len(data) == 1:
        ((tag, inner),) = data.items()
        if tag == "id":
            if not isinstance(inner, str):
                raise SchemaError(f"Ref type must be a type id string, got {inner!r}")
            return IdSchema(inner)
        if tag == "list":
            return ListSchema(schema_from_wire(inner))
        if tag == "nullable":
            return NullableSchema(schema_from_wire(inner))
        if tag == "struct":
            if not isinstance(inner, Mapping):
                raise SchemaError(f"Struct fields must be a mapping, got {type(inner).__name__}")
            return StructSchema(
                frozendict({name: schema_from_wire(s) for name, s in inner.items()})
            )
    raise SchemaError(f"Invalid schema data: {data!r}")


def is_assignable(provided: Schema, expected: Schema) -> bool:
    """Whether a value of schema ``provided`` may be used where ``expected`` is required.

    Schemas must match exactly, except that ``expected`` may be nullable where ``provided`` is
    not, and may be an untyped ``id`` where ``provided`` is a typed id. The rules apply
    recursively through list elements and struct fields, so ``list[str]`` is assignable to
    ``list[str?]`` and ``struct{a: int}`` to ``struct{a: int?}``.
    """
    match expected:
        case NullableSchema(inner):
            if isinstance(provided, NullableSchema):
                return is_assignable(provided.inner, inner)
            return is_assignable(provided, inner)
        case LeafSchema():
            return provided == expected
        case IdSchema(None):
            return isinstance(provided, IdSchema)
        case IdSchema():
            return provided == expected
        case ListSchema(element):
            return isinstance(provided, ListSchema) and is_assignable(provided.element, element)
        case StructSchema(fields):
            return (
                isinstance(provided, StructSchema)
                and set(provided.fields) == set(fields)
                and all(is_assignable(provided.fields[n], fields[n]) for n in fields)
            )


# ---- values ----

type FrozenValue = (
    None
    | int
    | float
    | bool
    | str
    | bytes
    | EntityID
    | tuple[FrozenValue, ...]
    | frozendict[str, FrozenValue]
)
"""A frozen value conforming to some schema: the shape arrays hold and hand out.

Entity references are :class:`~fxtr.core.entity_data.EntityID`\\ s."""

type ValueInput = (
    None
    | int
    | float
    | bool
    | str
    | bytes
    | EntityID
    | BoundID[Any]
    | Sequence[ValueInput]
    | Mapping[str, ValueInput]
)
"""The wide input shape for values: any sequence/mapping implementation is accepted and frozen,
and a bound id is accepted for an id (only the id is kept)."""

LEAF_PYTHON_TYPES: dict[str, type] = {
    "int": int,
    "float": float,
    "bool": bool,
    "str": str,
    "bytes": bytes,
}
"""The one Python type a frozen value of each leaf kind has, exactly: no subclasses, and a bool
is not an int."""


def coerce_value(value: object, schema: Schema, where: str = "value") -> FrozenValue:
    """Check ``value`` against ``schema`` strictly and return it frozen.

    Leaves must have the exact Python type (``bool`` is not an ``int``, ``int`` is not a
    ``float``); an id accepts an :class:`~fxtr.core.entity_data.EntityID` or a
    :class:`~fxtr.core.entities.BoundID` and keeps only its entity id, without checking the
    referenced entity's type.
    Sequences (other than ``str`` / ``bytes``) become tuples and mappings become frozendicts with
    fields sorted by name.

    Args:
        value: The value to check.
        schema: The schema it must match.
        where: A description of the value's location, for error messages.

    Raises:
        ValueSchemaError: If the value does not match.
    """
    match schema:
        case NullableSchema(inner):
            return None if value is None else coerce_value(value, inner, where)
        case IdSchema():
            if isinstance(value, BoundID):
                value = value.entity_id
            if isinstance(value, EntityID):
                return value
            raise ValueSchemaError(f"{where}: expected an entity id, got {type(value).__name__}")
        case LeafSchema(kind):
            if type(value) is LEAF_PYTHON_TYPES[kind]:
                return value  # type: ignore[return-value]
            raise ValueSchemaError(f"{where}: expected {kind}, got {type(value).__name__}")
        case ListSchema(element):
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ValueSchemaError(f"{where}: expected a sequence, got {type(value).__name__}")
            return tuple(
                coerce_value(item, element, f"{where}[{i}]") for i, item in enumerate(value)
            )
        case StructSchema(fields):
            if not isinstance(value, Mapping):
                raise ValueSchemaError(f"{where}: expected a mapping, got {type(value).__name__}")
            if set(value) != set(fields):
                raise ValueSchemaError(
                    f"{where}: expected fields {sorted(fields)}, got {sorted(value)}"
                )
            return frozendict(
                {
                    name: coerce_value(value[name], fields[name], f"{where}.{name}")
                    for name in sorted(fields)
                }
            )


def value_to_wire(value: FrozenValue) -> FxtrDataValue:
    """Thaw a frozen value into the mutable wire shape (tuples to lists, frozendicts to dicts)."""
    match value:
        case tuple():
            return [value_to_wire(v) for v in value]
        case frozendict():
            return {k: value_to_wire(v) for k, v in value.items()}
        case _:
            return value  # type: ignore[return-value]


# ---- presentation ----


def needs_presentation(schema: Schema) -> bool:
    """Whether :func:`present_value` changes anything for values of ``schema``: some id in it has
    a loader, some struct a TypedDict, or some list presents as a ``list``."""
    match schema:
        case LeafSchema():
            return False
        case IdSchema(_, loader):
            return loader is not None
        case NullableSchema(inner):
            return needs_presentation(inner)
        case ListSchema(element, present_as):
            return present_as is not tuple or needs_presentation(element)
        case StructSchema(fields, python_type):
            return python_type is not None or any(needs_presentation(f) for f in fields.values())


def present_value(value: FrozenValue, schema: Schema) -> Any:
    """The *presented* form of a frozen value: what an accessor hands out.

    Ids with a loader come back bound (:meth:`~fxtr.core.entity_data.EntityID.bind_loader`),
    structs with a TypedDict come back as fresh ``dict``\\ s, lists declared ``list`` come back as
    fresh ``list``\\ s; everything else stays in its frozen form. The result is built anew on
    every call (dicts and lists are mutable), so mutating it never touches the frozen value.
    Check :func:`needs_presentation` first to skip the walk when nothing would change.
    """
    match schema:
        case LeafSchema():
            return value
        case IdSchema(_, loader):
            if loader is None:
                return value
            assert isinstance(value, EntityID)
            return value.bind_loader(loader)
        case NullableSchema(inner):
            return None if value is None else present_value(value, inner)
        case ListSchema(element, present_as):
            assert isinstance(value, tuple)
            return present_as(present_value(item, element) for item in value)
        case StructSchema(fields, python_type):
            assert isinstance(value, frozendict)
            out = {name: present_value(value[name], fields[name]) for name in sorted(fields)}
            return out if python_type is not None else frozendict(out)


def strip_presentation(schema: Schema) -> Schema:
    """The same schema with all presentation removed: ids lose their loader, lists present as
    tuples, structs drop their TypedDict.

    Presentation is off the wire and takes no part in equality or hashing, so the result is equal
    to ``schema`` and :func:`needs_presentation` of it is ``False``. A workflow context strips the
    types and source arrays it emits this way (see
    :class:`fxtr.runner.contexts.RunnerWorkflowContext`), so a backend only ever holds
    presentation-free types; the presented form is re-applied worker-side
    from the type a handle carries. The unchanged schema is returned when there is nothing to strip.
    """
    if not needs_presentation(schema):
        return schema
    match schema:
        case IdSchema(type_id, _):
            return IdSchema(type_id, None)
        case NullableSchema(inner):
            return NullableSchema(strip_presentation(inner))
        case ListSchema(element, _):
            return ListSchema(strip_presentation(element))
        case StructSchema(fields, _):
            return StructSchema(frozendict({n: strip_presentation(s) for n, s in fields.items()}))
        case _:
            return schema


# ---- schema inference ----


@dataclasses.dataclass
class _Partial:
    """A partially inferred schema; ``kind`` is unknown while only nulls/empties are seen."""

    kind: Literal["unknown", "leaf", "id", "list", "struct"] = "unknown"
    nullable: bool = False
    leaf: str | None = None
    type_id: str | None = None
    loader: EntityLoader[Any] | None = None
    element: _Partial | None = None
    fields: dict[str, _Partial] | None = None


def _infer_partial(value: object, where: str) -> _Partial:
    if value is None:
        return _Partial(nullable=True)
    if isinstance(value, BoundID):
        type_id = value.loader.static_type_id
        if type_id is None:
            raise ValueSchemaError(
                f"{where}: cannot infer a schema for a bound id whose loader does not name a"
                " single entity type; give an explicit schema"
            )
        return _Partial(kind="id", type_id=type_id, loader=value.loader)
    if isinstance(value, EntityID):
        raise ValueSchemaError(
            f"{where}: cannot infer a schema for an entity id, since the id does not determine"
            " the type of the entity it points to; give an explicit schema (id_of(...)), or bind"
            " the id to its type"
        )
    for kind, py_type in LEAF_PYTHON_TYPES.items():
        if type(value) is py_type:
            return _Partial(kind="leaf", leaf=kind)
    if isinstance(value, Mapping):
        return _Partial(
            kind="struct",
            fields={k: _infer_partial(v, f"{where}.{k}") for k, v in value.items()},
        )
    if isinstance(value, Sequence):
        element = _Partial()
        for i, item in enumerate(value):
            element = _unify_partial(element, _infer_partial(item, f"{where}[{i}]"), where)
        return _Partial(kind="list", element=element)
    raise ValueSchemaError(f"{where}: cannot infer a schema for {type(value).__name__}")


def _unify_partial(a: _Partial, b: _Partial, where: str) -> _Partial:
    nullable = a.nullable or b.nullable
    if a.kind == "unknown":
        return dataclasses.replace(b, nullable=nullable)
    if b.kind == "unknown":
        return dataclasses.replace(a, nullable=nullable)
    if (
        a.kind != b.kind
        or (a.kind == "leaf" and a.leaf != b.leaf)
        or (a.kind == "id" and a.type_id != b.type_id)
    ):
        raise ValueSchemaError(
            f"{where}: conflicting schemas {_partial_str(a)} and {_partial_str(b)}"
        )
    if a.kind == "leaf":
        return dataclasses.replace(a, nullable=nullable)
    if a.kind == "id":
        return dataclasses.replace(a, nullable=nullable, loader=a.loader or b.loader)
    if a.kind == "list":
        assert a.element is not None and b.element is not None
        return _Partial(
            kind="list", nullable=nullable, element=_unify_partial(a.element, b.element, where)
        )
    assert a.fields is not None and b.fields is not None
    if set(a.fields) != set(b.fields):
        raise ValueSchemaError(
            f"{where}: conflicting struct fields {sorted(a.fields)} and {sorted(b.fields)}"
        )
    return _Partial(
        kind="struct",
        nullable=nullable,
        fields={k: _unify_partial(a.fields[k], b.fields[k], f"{where}.{k}") for k in a.fields},
    )


def _partial_str(p: _Partial) -> str:
    match p.kind:
        case "unknown":
            base = "?"
        case "leaf":
            base = str(p.leaf)
        case "id":
            base = str(IdSchema(p.type_id))
        case "list":
            assert p.element is not None
            base = f"list[{_partial_str(p.element)}]"
        case "struct":
            assert p.fields is not None
            base = (
                "struct{"
                + ", ".join(f"{k}: {_partial_str(p.fields[k])}" for k in sorted(p.fields))
                + "}"
            )
    return base + ("?" if p.nullable else "")


def _finalize_partial(p: _Partial, where: str) -> Schema:
    base: Schema
    match p.kind:
        case "unknown":
            raise ValueSchemaError(
                f"{where}: cannot infer a schema (only nulls or empty lists were seen); give an"
                " explicit schema"
            )
        case "leaf":
            base = LeafSchema(p.leaf)  # type: ignore[arg-type]
        case "id":
            base = IdSchema(p.type_id, p.loader)
        case "list":
            assert p.element is not None
            base = ListSchema(_finalize_partial(p.element, f"{where}[]"))
        case "struct":
            assert p.fields is not None
            base = StructSchema(
                frozendict({k: _finalize_partial(v, f"{where}.{k}") for k, v in p.fields.items()})
            )
    return NullableSchema(base) if p.nullable else base


def infer_schema(value: object, where: str = "value") -> Schema:
    """Infer the narrowest schema matching ``value``.

    Rules: leaves are typed by their exact Python type (``True`` is a ``bool``, never an ``int``);
    list elements are unified, and ``None`` among them makes the element schema nullable; structs
    unify fieldwise across list elements and must then have identical field names. A position seen
    only as ``None`` or only inside empty lists has no schema and is an error (this includes a
    top-level ``None`` or ``[]``); give an explicit schema instead. Bare entity ids are also an
    error: an id does not say what type of entity it points to, so id schemas must be declared
    explicitly (:func:`id_of`). A :class:`~fxtr.core.entities.BoundID` whose loader
    names its entity type (:attr:`~fxtr.core.entities.EntityLoader.static_type_id`) infers a typed
    id carrying that loader.

    Args:
        value: The value to infer from.
        where: A description of the value, for error messages.

    Raises:
        ValueSchemaError: If the value's parts conflict or leave a schema undetermined.
    """
    return _finalize_partial(_infer_partial(value, where), where)
