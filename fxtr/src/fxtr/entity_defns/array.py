"""Arrays: sparse named arrays of regular data, storable as entities.

A :class:`Array` is a mapping from *keys* to *values*. Its :class:`Dim`\\ s (dimensions) are named,
ordered key columns, each holding ``int`` or ``str`` keys; a key is one value per dim, and keys are
unique. Its *value* is anonymous and regular, described by a single
:data:`~fxtr.entity_defns.schema.Schema`. So logically an array is ``{(k1, ..., kn): value}``, with
its static :class:`ArrayType` being the dims plus the value schema:

* a **scalar** is an array with no dims, holding exactly one value;
* a conventional **multi-column array** is an array whose value schema is a struct, the struct
  fields being its columns;
* a **nested** or **ragged** collection (say, several samples per prompt) is an array with more dims
  (``[prompt, sample]``), sparse in that its keys need not form a full cartesian product.

Because every array has this shape, arrays compose by dim name: :func:`join_keys` lines several
arrays up on shared dims, with arrays lacking a dim *broadcast* along it, either requiring key sets
to match exactly (``join="exact"``) or intersecting them (``join="inner"``); :meth:`Array.combine`
uses it to zip arrays into one struct-valued array, and :meth:`Array.stack` adds a dim by stacking
same-typed arrays. The lazy queries :meth:`Array.sel` and :meth:`Array.rename` slice along dims
and rename them. Dim order is presentation only: it fixes key tuple order and canonical row
order, and arrays with the same data in a different dim order are unequal, but every operation
matches dims by name.

Values are checked strictly against the schema and frozen (see
:func:`~fxtr.entity_defns.schema.coerce_value`); rows are sorted by key, so two arrays with the
same type and rows are equal and have the same content id however they were built. The wire form
is::

    {
        "$type": "org.transluce.fxtr.core.Array",
        "dims": [[name, kind], ...],
        "schema": <schema>,
        "rows": [[k1, ..., kn, value], ...],   # sorted by key
    }

and loading rejects data that is not in this canonical form.
"""

from __future__ import annotations

import dataclasses
import itertools
import re
import threading
import typing
import weakref
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Generic,
    Literal,
    TypeVar,
    cast,
    get_args,
    overload,
)

import cattrs
from cattrs.dispatch import StructureHook, UnstructureHook
from frozendict import frozendict

from fxtr.core.codec import iter_entity_ids
from fxtr.core.entity_data import (
    TYPE_KEY,
    EntityID,
    FxtrDataValue,
    StorableFxtrDataValue,
)
from fxtr.entity_defns.dataclass_bases import StrictTypeError
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.schema import (
    FrozenValue,
    LeafSchema,
    ListSchema,
    NullableSchema,
    Schema,
    SchemaError,
    SchemaSpec,
    StructSchema,
    ValueSchemaError,
    as_schema,
    coerce_value,
    contains_entity_ids,
    infer_schema,
    is_assignable,
    is_schema,
    needs_presentation,
    present_value,
    schema_from_annotation,
    schema_from_wire,
    schema_to_wire,
    strip_presentation,
    value_to_wire,
)

if TYPE_CHECKING:
    import anyio
    import pyarrow as pa

    from fxtr.duckdb_engine.executor import DuckDBExecutionLimits
    from fxtr.duckdb_engine.local_query import ArrayQuery
    from fxtr.entity_defns.query import (
        Aggregation,
        Condition,
        JoinOn,
        OrderBy,
        TiePolicy,
        ValueExpression,
    )

ARRAY_TYPE = "org.transluce.fxtr.core.Array"
"""The type id of :class:`Array` entities."""


class ArrayError(ValueError):
    """An array's rows are invalid, or an operation on arrays fails."""


class ArrayTypeError(ArrayError):
    """An array type (dims or schema) is malformed, or array types do not fit together."""


type Join = Literal["exact", "inner"]
"""How key sets are reconciled when arrays are lined up along shared dims: ``"exact"`` requires
them to agree, ``"inner"`` keeps the keys present in all inputs (see :func:`join_keys`)."""

type CombineJoin = Literal["exact", "inner", "outer"]
""":data:`Join` plus ``"outer"``, the union of keys, for :meth:`Array.combine` (where a missing
value becomes ``None``)."""


# ---- dims and array types ----

type DimKind = Literal["int", "str"]
"""The kinds of keys a dim can have."""

DIM_KINDS: frozenset[str] = frozenset(get_args(DimKind.__value__))

type KeyValue = int | str
"""A key value along a dim (see :data:`DimKind`)."""

_DIM_PYTHON_TYPES: dict[str, type] = {"int": int, "str": str}


@dataclasses.dataclass(frozen=True)
class Dim:
    """A named dimension of an array, with the kind of its keys."""

    name: str
    kind: DimKind

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ArrayTypeError(f"Dim names must be non-empty strings, got {self.name!r}")
        if not isinstance(self.kind, str) or self.kind not in DIM_KINDS:
            raise ArrayTypeError(
                f"Dim {self.name!r}: kind must be one of {sorted(DIM_KINDS)}, got {self.kind!r}"
            )

    def __str__(self) -> str:
        return f"{self.name}: {self.kind}"


type Dims = tuple[Dim, ...]
"""An array's dims, in order; names are unique."""

type DimsSpec = str | Sequence[Dim] | Mapping[str, DimKind]
"""Dims as a DSL string (``"prompt: int, sample: str"``), a sequence of dims, or a name-to-kind
mapping."""


_DIM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_dims(spec: str) -> Dims:
    """Parse dims from DSL form: ``"prompt: int, sample: str"``, optionally in brackets
    (``"[prompt: int, sample: str]"``); ``""`` and ``"[]"`` are no dims. A trailing comma is
    allowed; whitespace is insignificant.

    Raises:
        ArrayTypeError: If the string does not parse, a name repeats, or a kind is not a key kind.
    """
    text = spec.strip()
    if text.startswith("["):
        if not text.endswith("]"):
            raise ArrayTypeError(f"Unclosed '[' in dims {spec!r}")
        text = text[1:-1].strip()
    if not text:
        return ()
    parts = text.split(",")
    if parts[-1].strip() == "":
        parts.pop()
    dims: list[Dim] = []
    for part in parts:
        name, sep, kind = (s.strip() for s in part.partition(":"))
        if not sep or _DIM_NAME_RE.fullmatch(name) is None:
            raise ArrayTypeError(
                f"Expected 'name: kind' for a dim, got {part.strip()!r} in {spec!r}"
            )
        if kind not in DIM_KINDS:
            raise ArrayTypeError(
                f"Dim {name!r}: kind must be one of {sorted(DIM_KINDS)}, got {kind!r}"
            )
        dims.append(Dim(name, kind))  # type: ignore[arg-type]
    return as_dims(dims)


def _key_kind(schema: Schema, where: str) -> DimKind:
    if isinstance(schema, LeafSchema) and schema.kind in DIM_KINDS:
        return schema.kind  # type: ignore[return-value]
    raise ArrayTypeError(f"{where}: kind must be one of {sorted(DIM_KINDS)}, got {schema}")


def validate_dims(dims: Dims) -> Dims:
    """Check that ``dims`` is already :data:`Dims` — a tuple of :class:`Dim`\\ s with unique names —
    and return it unchanged.

    Unlike :func:`as_dims` this does not normalize a :data:`DimsSpec`; it is what :class:`ArrayType`
    and :class:`PartialArrayType` use to validate their ``dims`` field, whose :meth:`of`
    constructors are the spec-normalizing entry points.

    Raises:
        ArrayTypeError: If a name repeats or an element is not a :class:`Dim`.
    """
    seen: set[str] = set()
    for dim in dims:
        if not isinstance(dim, Dim):
            raise ArrayTypeError(f"Expected a Dim, got {dim!r}")
        if dim.name in seen:
            raise ArrayTypeError(f"Duplicate dim name {dim.name!r}")
        seen.add(dim.name)
    return dims


def as_dims(spec: DimsSpec) -> Dims:
    """Normalize a :data:`DimsSpec` to :data:`Dims`, checking that names are unique.

    Raises:
        ArrayTypeError: If a name repeats or an element is not a :class:`Dim`.
    """
    if isinstance(spec, str):
        return parse_dims(spec)
    dims = (
        tuple(Dim(name, kind) for name, kind in spec.items())
        if isinstance(spec, Mapping)
        else tuple(spec)
    )
    return validate_dims(dims)


def union_dims(groups: Iterable[Dims]) -> Dims:
    """The union of several dim tuples, in order of first appearance, matching dims by name.

    Raises:
        ArrayTypeError: If two groups give a dim of the same name different kinds.
    """
    out: dict[str, Dim] = {}
    for dims in groups:
        for dim in dims:
            if dim.name in out and out[dim.name] != dim:
                raise ArrayTypeError(
                    f"Dim {dim.name!r} has kind {out[dim.name].kind} in one array and"
                    f" {dim.kind} in another"
                )
            out.setdefault(dim.name, dim)
    return tuple(out.values())


@dataclasses.dataclass(frozen=True)
class ArrayType:
    """The static type of an array: its dims and its value schema.

    Built from an :data:`ArrayTypeSpec` by :meth:`of`: ``("[prompt: int, sample: int]", str)``,
    or just ``str`` for a scalar. Displayed as ``[dims]: schema``.
    """

    dims: Dims
    schema: Schema

    def __post_init__(self) -> None:
        validate_dims(self.dims)
        if not is_schema(self.schema):
            raise ArrayTypeError(f"Expected a Schema, got {self.schema!r}")

    @classmethod
    def of(cls, spec: ArrayTypeSpec) -> ArrayType:
        """Normalize a :data:`ArrayTypeSpec`: an array type, a ``(dims, schema)`` pair of specs,
        or a bare schema spec for a scalar.

        Raises:
            ArrayTypeError: If the spec is malformed (a string, which the value schema has no
                text form for; bad dims; a value that is not a schema annotation).
        """
        if isinstance(spec, ArrayType):
            return spec
        if isinstance(spec, str):
            raise ArrayTypeError(
                f"An array type is not written as a string ({spec!r}); give (dims, schema) with"
                " the dims as a string and the schema as a Schema or a type annotation, or a"
                " bare schema for a scalar"
            )
        try:
            if isinstance(spec, tuple):
                dims, schema = spec
                return cls(dims=as_dims(dims), schema=as_schema(schema))
            return cls(dims=(), schema=as_schema(spec))
        except SchemaError as exc:
            raise ArrayTypeError(str(exc)) from exc

    @property
    def dim_names(self) -> tuple[str, ...]:
        return tuple(dim.name for dim in self.dims)

    def dim(self, name: str) -> Dim:
        """The dim called ``name``.

        Raises:
            KeyError: If there is no such dim.
        """
        for dim in self.dims:
            if dim.name == name:
                return dim
        raise KeyError(name)

    def strip_presentation(self) -> ArrayType:
        """This type with presentation removed from its schema (see
        :func:`~fxtr.entity_defns.schema.strip_presentation`); unchanged if there is none."""
        schema = strip_presentation(self.schema)
        return self if schema is self.schema else ArrayType(self.dims, schema)

    def without(self, names: Iterable[str]) -> ArrayType:
        """This type with the named dims removed (names not present are ignored)."""
        drop = set(names)
        return ArrayType(tuple(d for d in self.dims if d.name not in drop), self.schema)

    def __str__(self) -> str:
        return f"[{', '.join(map(str, self.dims))}]: {self.schema}"

    def to_wire(self) -> dict[str, StorableFxtrDataValue]:
        return {"dims": _dims_to_wire(self.dims), "schema": schema_to_wire(self.schema)}

    @classmethod
    def from_wire(cls, data: object) -> ArrayType:
        """Deserialize an array type (strictly) from its wire form.

        Raises:
            ArrayTypeError: If the data is not a valid array type.
        """
        if not isinstance(data, Mapping) or set(data) != {"dims", "schema"}:
            raise ArrayTypeError(
                f"Array type data must have exactly 'dims' and 'schema', got {data!r}"
            )
        try:
            schema = schema_from_wire(data["schema"])
        except ValueError as exc:
            raise ArrayTypeError(str(exc)) from exc
        return cls(dims=_dims_from_wire(data["dims"]), schema=schema)

    # ---- serialization hooks (see fxtr.entity_defns.dataclass_bases) ----
    #
    # An ArrayType is a value type, not an entity: as a field of a tagged dataclass it serializes
    # as its wire form, ``{"dims": ..., "schema": ...}``, the same two keys an Array spreads into
    # its own wire form.

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        def hook(ttype: ArrayType) -> dict[str, Any]:
            return ttype.to_wire()

        return hook

    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        def hook(value: Any, _target: Any) -> ArrayType:
            try:
                return ArrayType.from_wire(value)
            except ArrayTypeError as exc:
                raise StrictTypeError(str(exc)) from exc

        return hook


type ArrayTypeSpec = ArrayType | tuple[DimsSpec, SchemaSpec] | SchemaSpec
"""An array type: a :class:`ArrayType`; a ``(dims, schema)`` pair of :data:`DimsSpec` and
:data:`~fxtr.entity_defns.schema.SchemaSpec`, e.g. ``("[sample: int]", str)``; or a bare
:data:`~fxtr.entity_defns.schema.SchemaSpec` (a schema or a type annotation such as ``int`` or
``BoundID[Bio]``) for a scalar. Normalized by :meth:`ArrayType.of`."""


# ---- partial array types ----


@dataclasses.dataclass(frozen=True)
class PartialArrayType:
    """An array type with the value schema optionally left out.

    Written as a dims string alone, ``"[model: str, sample: int]"``: it names the dims an array
    must have and says nothing about its values. This is for *asserting* a type that is declared
    elsewhere — see
    :meth:`Array.expect_type` — where naming the dims is the point and repeating the schema would
    be noise. A partial type with a schema is just a :class:`ArrayType` in another wrapper.

    Attributes:
        dims: The dims the array must have, in order.
        schema: The value schema it must have, or ``None`` to leave the values unconstrained.
    """

    dims: Dims
    schema: Schema | None = None

    def __post_init__(self) -> None:
        validate_dims(self.dims)
        if self.schema is not None and not is_schema(self.schema):
            raise ArrayTypeError(f"Expected a Schema or None, got {self.schema!r}")

    @classmethod
    def of(cls, spec: PartialArrayTypeSpec) -> PartialArrayType:
        """Normalize a :data:`PartialArrayTypeSpec`: a string is dims only, a pair is
        ``(dims, schema or None)``, anything else is an :data:`ArrayTypeSpec`.

        Raises:
            ArrayTypeError: If the spec is malformed.
        """
        if isinstance(spec, PartialArrayType):
            return spec
        if isinstance(spec, str):
            return cls(dims=parse_dims(spec))
        if isinstance(spec, tuple):
            dims, schema = spec
            try:
                return cls(dims=as_dims(dims), schema=None if schema is None else as_schema(schema))
            except SchemaError as exc:
                raise ArrayTypeError(str(exc)) from exc
        full = ArrayType.of(spec)
        return cls(dims=full.dims, schema=full.schema)

    def complete(self, where: str = "array type") -> ArrayType:
        """This as a :class:`ArrayType`.

        Raises:
            ArrayTypeError: If the value schema was left out.
        """
        if self.schema is None:
            raise ArrayTypeError(f"{where}: {self} names dims but no value schema")
        return ArrayType(dims=self.dims, schema=self.schema)

    def matches(self, type: ArrayType) -> bool:
        """Whether ``type`` is exactly this: the same dims, in the same order, and an equal value
        schema when one is given.

        This is deliberately stricter than :func:`is_assignable_array`, which compares dims as a
        set: a partial type is written down to *document* an array, and dim order is part of what
        it documents.
        """
        return type.dims == self.dims and (self.schema is None or type.schema == self.schema)

    def check(self, type: ArrayType, where: str = "Array") -> None:
        """Raise unless ``type`` :meth:`matches`.

        Raises:
            ArrayTypeError: If it does not.
        """
        if not self.matches(type):
            raise ArrayTypeError(f"{where}: expected {self}, got {type}")

    def __str__(self) -> str:
        dims = f"[{', '.join(map(str, self.dims))}]"
        return dims if self.schema is None else f"{dims}: {self.schema}"


type PartialArrayTypeSpec = (
    str | PartialArrayType | tuple[DimsSpec, SchemaSpec | None] | ArrayTypeSpec
)
"""A :data:`ArrayTypeSpec`; a dims string alone (``"[model: str, sample: int]"``), leaving the
value schema out; or a ``(dims, None)`` pair doing the same. Normalized by
:meth:`PartialArrayType.of`."""


def as_partial_array_type(spec: PartialArrayTypeSpec) -> PartialArrayType:
    """Normalize a :data:`PartialArrayTypeSpec` (same as :meth:`PartialArrayType.of`)."""
    return PartialArrayType.of(spec)


def as_array_type(spec: ArrayTypeSpec) -> ArrayType:
    """Normalize a :data:`ArrayTypeSpec` to a :class:`ArrayType` (same as :meth:`ArrayType.of`)."""
    return ArrayType.of(spec)


def is_assignable_array(provided: ArrayType, expected: ArrayType) -> bool:
    """Whether an array of type ``provided`` may be used where ``expected`` is required: the same
    dims (by name and kind, in any order) and an assignable value schema."""
    return set(provided.dims) == set(expected.dims) and is_assignable(
        provided.schema, expected.schema
    )


def field_type(array: ArrayType, name: str) -> ArrayType:
    """The type of :meth:`Array.field`: the same dims, and the schema of struct field ``name``.

    A nullable struct yields a nullable field.

    Raises:
        ArrayTypeError: If the schema is not a struct or has no such field.
    """
    schema = array.schema
    nullable = isinstance(schema, NullableSchema)
    struct = schema.inner if isinstance(schema, NullableSchema) else schema
    if not isinstance(struct, StructSchema):
        raise ArrayTypeError(f"Cannot take field {name!r} of a non-struct value ({schema})")
    if name not in struct.fields:
        raise ArrayTypeError(f"No field {name!r} in {struct}")
    field = struct.fields[name]
    if nullable and not isinstance(field, NullableSchema):
        field = NullableSchema(field)
    return ArrayType(array.dims, field)


def rename_type(array: ArrayType, names: Mapping[str, str]) -> ArrayType:
    """The type of :meth:`Array.rename`: the same type with dims renamed (old name to new name),
    keeping their order and kinds.

    Raises:
        ArrayTypeError: If an old name does not exist, or the renamed dims are not unique.
    """
    missing = [n for n in names if n not in array.dim_names]
    if missing:
        raise ArrayTypeError(f"No dims {missing} in {array}")
    return ArrayType(
        tuple(Dim(names.get(d.name, d.name), d.kind) for d in array.dims), array.schema
    )


def combine_type(inputs: Mapping[str, ArrayType], join: CombineJoin = "exact") -> ArrayType:
    """The type of :meth:`Array.combine`: the union of the inputs' dims (in order of first
    appearance) and a struct with one field per input. With ``join="outer"`` every field is
    nullable, since an input may have no value at a result key.

    Raises:
        ArrayTypeError: If two inputs disagree on a dim's kind.
    """

    def field(schema: Schema) -> Schema:
        if join == "outer" and not isinstance(schema, NullableSchema):
            return NullableSchema(schema)
        return schema

    return ArrayType(
        union_dims(t.dims for t in inputs.values()),
        StructSchema(frozendict({name: field(t.schema) for name, t in inputs.items()})),
    )


def sel_type(array: ArrayType, names: Iterable[str]) -> ArrayType:
    """The type of :meth:`Array.sel` along the named dims: the same type with those dims removed.

    Raises:
        ArrayTypeError: If a named dim does not exist.
    """
    missing = [n for n in names if n not in array.dim_names]
    if missing:
        raise ArrayTypeError(f"No dims {missing} in {array}")
    return array.without(names)


def stack_dim(keys: Iterable[KeyValue], dim: str) -> Dim:
    """The dim :meth:`Array.stack` adds: named ``dim``, with the kind of ``keys``, which must all
    be ``int`` or all be ``str`` (and there must be at least one).

    Raises:
        ArrayTypeError: If there are no keys or they are not all of one key kind.
    """
    kinds = {type(k).__name__ for k in keys}
    if kinds > {"int", "str"} or len(kinds) != 1:
        raise ArrayTypeError(
            f"Stack keys must all be int or all be str, got kinds {sorted(kinds) or 'none'}"
        )
    return Dim(dim, kinds.pop())  # type: ignore[arg-type]


def stack_over_type(types: Iterable[ArrayType], dims: Dims) -> ArrayType:
    """The type of :meth:`Array.stack_over`: the common type of the inputs with ``dims``
    prepended.

    Inputs must have the same dims (by name and kind, in any order) and the same schema.

    Raises:
        ArrayTypeError: If there are no inputs, they differ in dims or schema, or a stacked dim
            collides with one of theirs.
    """
    it = iter(types)
    try:
        first = next(it)
    except StopIteration:
        raise ArrayTypeError("Cannot stack zero arrays") from None
    for other in it:
        if set(other.dims) != set(first.dims) or other.schema != first.schema:
            raise ArrayTypeError(f"Cannot stack arrays of types {first} and {other}")
    collisions = [d.name for d in dims if d.name in first.dim_names]
    if collisions:
        raise ArrayTypeError(f"Stacked dims {collisions} collide with existing dims")
    return ArrayType((*dims, *first.dims), first.schema)


def stack_type(types: Iterable[ArrayType], dim: Dim) -> ArrayType:
    """The type of :meth:`Array.stack`: :func:`stack_over_type` with the single dim ``dim``."""
    return stack_over_type(types, (dim,))


def transpose_type(array: ArrayType, names: Sequence[str]) -> ArrayType:
    """The type of :meth:`Array.transpose`: the same type with dims in the order of ``names``.

    Raises:
        ArrayTypeError: If ``names`` is not a permutation of the array's dim names.
    """
    if sorted(names) != sorted(array.dim_names):
        raise ArrayTypeError(
            f"transpose needs a permutation of dims {list(array.dim_names)}, got {list(names)}"
        )
    return ArrayType(tuple(array.dim(n) for n in names), array.schema)


def _dims_to_wire(dims: Dims) -> list[FxtrDataValue]:
    return [[dim.name, dim.kind] for dim in dims]


def _dims_from_wire(data: object) -> Dims:
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        raise ArrayTypeError(f"Dims must be a list, got {type(data).__name__}")
    dims: list[Dim] = []
    for item in data:
        if not (isinstance(item, Sequence) and len(item) == 2 and isinstance(item[0], str)):
            raise ArrayTypeError(f"Dim must be a [name, kind] pair, got {item!r}")
        dims.append(Dim(item[0], item[1]))
    return as_dims(dims)


def _infer_common(values: Sequence[object], where: str) -> Schema:
    """Infer the schema shared by all of ``values`` (see :func:`infer_schema`).

    Raises:
        ArrayError: If the values conflict or leave the schema undetermined.
    """
    try:
        column = infer_schema(list(values), where)
    except ValueError as exc:
        raise ArrayError(str(exc)) from exc
    assert isinstance(column, ListSchema)
    return column.element


def _infer_column(records: Sequence[Mapping[str, Any]], name: str, where: str) -> Schema:
    """Infer the schema shared by one column of ``records``."""
    return _infer_common([r[name] for r in records], where)


# ---- keys ----

type Key = tuple[KeyValue, ...]
"""An array key: one key value per dim, in dim order."""

type KeySpec = Sequence[KeyValue] | Mapping[str, KeyValue]
"""A key as a tuple in dim order or as a name-to-value mapping."""

type Row[V] = tuple[Key, V]
"""One array row: a key and its value."""


def join_keys(
    inputs: Mapping[str, Array[Any]], dims: Dims, join: CombineJoin = "exact"
) -> list[Key]:
    """The keys over ``dims`` shared by several arrays, matching dims by name.

    Each input takes part only through the dims it has among ``dims``; an input with none of them
    is broadcast along all of them and constrains nothing. The result is the set of keys over
    ``dims`` whose projection onto each input's participating dims is one of that input's keys
    (so inputs on disjoint dims give a cartesian product), sorted.

    With ``join="exact"``, every input's keys (projected onto its participating dims) must be
    exactly the projection of the result; that is, inputs must agree on the key set of every
    shared dim. With ``join="inner"`` the intersection is taken silently. With ``join="outer"``
    the union is taken instead: each input's keys are broadcast along the dims it lacks using the
    union of every input's labels there, and the results are merged, so some inputs have no value
    at some result keys.

    Args:
        inputs: The arrays, by name (used in error messages).
        dims: The dims to join over. Each input's dims of the same name must have the same kind.
        join: The reconciliation mode.

    Raises:
        ArrayTypeError: If an input has a dim of the same name but different kind.
        ArrayError: If ``join="exact"`` and key sets disagree.
    """
    target = [d.name for d in dims]
    for name, array in inputs.items():
        for dim in array.dims:
            if dim.name in target and dims[target.index(dim.name)] != dim:
                raise ArrayTypeError(
                    f"Input {name!r}: dim {dim.name!r} has kind {dim.kind}, expected"
                    f" {dims[target.index(dim.name)].kind}"
                )

    # Each input's participating dims, and its keys projected onto them.
    participating: dict[str, list[str]] = {}
    projected: dict[str, set[Key]] = {}
    for name, array in inputs.items():
        part = [d.name for d in array.dims if d.name in target]
        participating[name] = part
        positions = [array.dim_names.index(p) for p in part]
        projected[name] = {tuple(key[i] for i in positions) for key in array.keys()}

    uncovered = [d for d in target if not any(d in part for part in participating.values())]
    if uncovered:
        raise ArrayError(f"No input has dims {uncovered}, so their keys are undetermined")

    if join == "outer":
        labels: dict[str, set[KeyValue]] = {d: set() for d in target}
        for name in inputs:
            for i, p in enumerate(participating[name]):
                labels[p].update(key[i] for key in projected[name])
        union: set[Key] = set()
        for name in inputs:
            part = participating[name]
            others = [d for d in target if d not in part]
            for key in projected[name]:
                if not part:
                    continue
                base = dict(zip(part, key, strict=True))
                for combo in itertools.product(*(sorted(labels[d]) for d in others)):
                    full = {**base, **dict(zip(others, combo, strict=True))}
                    union.add(tuple(full[d] for d in target))
        return sorted(union)

    # Hash-join the inputs one at a time, growing the covered dims.
    covered: list[str] = []
    result: list[Key] = [()]
    for name in inputs:
        part = participating[name]
        if not part:
            continue
        shared = [p for p in part if p in covered]
        new = [p for p in part if p not in covered]
        shared_in_covered = [covered.index(p) for p in shared]
        shared_in_part = [part.index(p) for p in shared]
        new_in_part = [part.index(p) for p in new]
        by_shared: dict[Key, list[Key]] = {}
        for key in projected[name]:
            by_shared.setdefault(tuple(key[i] for i in shared_in_part), []).append(key)
        grown: list[Key] = []
        for key in result:
            for match in by_shared.get(tuple(key[i] for i in shared_in_covered), ()):
                grown.append(key + tuple(match[i] for i in new_in_part))
        result = grown
        covered.extend(new)

    if join == "exact":
        for name in inputs:
            part = participating[name]
            if not part:
                continue
            positions = [covered.index(p) for p in part]
            seen = {tuple(key[i] for i in positions) for key in result}
            if seen != projected[name]:
                extra = sorted(projected[name] - seen)[:3]
                raise ArrayError(
                    f"Input {name!r} has keys on dims {part} that other inputs lack, e.g."
                    f" {extra}; use join='inner' to intersect instead"
                )

    order = [covered.index(d) for d in target]
    return sorted(tuple(key[i] for i in order) for key in result)


# ---- the array ----


_HASH_CHUNK = 4096
"""Rows hashed together. The chunks start at fixed row positions, not at an Arrow table's batch
boundaries, so an array hashes the same whichever form it holds."""


@contextmanager
def _locked(lock: threading.Lock, check_cancelled: Callable[[], None] | None) -> Iterator[None]:
    """Hold ``lock``. A worker thread passes ``check_cancelled``, so that if its caller is
    cancelled while another thread holds the lock converting, it gives up instead of waiting."""
    if check_cancelled is None:
        lock.acquire()
    else:
        while not lock.acquire(timeout=0.05):
            check_cancelled()
    try:
        yield
    finally:
        lock.release()


class _Backing:
    """One logical array held as canonical Python rows, as its canonical Arrow table, or both.

    An array is built in one form: rows by the public constructors, Arrow by a query, a decoder,
    or an array operation (which runs on Arrow). The other form, the key index and the hash are
    derived from it the first time they are asked for and then kept. The form and index are each
    built at most once, under ``_lock``, because an array is shared by every thread that reads
    it; two threads may both compute the hash, and get the same value. Nothing else ever changes,
    so the array stays immutable. Arrays whose types are equal (type equality ignores
    presentation) may share a backing, since neither form depends on presentation.

    ``row_source`` is set for a row-preserving query's result: the input whose rows the result's
    are a subset of, held weakly so the result does not keep a large input alive. While the input
    is alive its row objects are shared rather than decoded.
    """

    __slots__ = (
        "arrow_conversions",
        "content_id",
        "python_materializations",
        "source",
        "type",
        "_hash",
        "_index",
        "_lock",
        "_row_source",
        "_rows",
        "_table",
    )

    def __init__(
        self,
        type: ArrayType,
        *,
        rows: tuple[Row[FrozenValue], ...] | None = None,
        index: dict[Key, int] | None = None,
        table: pa.Table | None = None,
        row_source: Array[Any] | None = None,
    ) -> None:
        if rows is None and table is None:
            raise ValueError("An array's backing needs its rows or its Arrow table")
        self.type = type
        self.source: Literal["rows", "arrow"] = "rows" if table is None else "arrow"
        """The form the array was built in, which pickling keeps."""
        self.arrow_conversions = 0
        self.python_materializations = 0
        self.content_id: EntityID | None = None
        """The array's content ID once :meth:`Array.content_id` has computed it."""
        self._rows = rows
        self._index = index
        self._table = table
        self._row_source = None if row_source is None else weakref.ref(row_source)
        self._hash: int | None = None
        self._lock = threading.Lock()

    @property
    def has_rows(self) -> bool:
        """Whether the complete tuple of rows is held."""
        return self._rows is not None

    @property
    def has_table(self) -> bool:
        """Whether the Arrow table is held."""
        return self._table is not None

    def __len__(self) -> int:
        if self._rows is not None:
            return len(self._rows)
        assert self._table is not None
        return self._table.num_rows

    def rows(self) -> tuple[Row[FrozenValue], ...]:
        """Every row, derived from the table and kept the first time they are asked for."""
        rows = self._rows
        if rows is None:
            with _locked(self._lock, None):
                rows = self._rows
                if rows is None:
                    rows = self._rows = self._derive_rows()
                    self.python_materializations += 1
        return rows

    def iter_rows(self) -> Iterator[Row[FrozenValue]]:
        """Every row in key order, decoded from the table a batch at a time when the rows are
        not held, so reading once does not keep them all."""
        if self._rows is not None:
            return iter(self._rows)
        from fxtr.entity_defns.arrow import arrow_rows

        assert self._table is not None
        return arrow_rows(self._table, self.type)

    def keys(self) -> Iterator[Key]:
        """Every key in order, decoding no values."""
        if self._rows is not None:
            return (key for key, _ in self._rows)
        from fxtr.entity_defns.arrow import arrow_keys

        assert self._table is not None
        return arrow_keys(self._table, self.type)

    def index(self) -> dict[Key, int]:
        """Each key's row position."""
        index = self._index
        if index is None:
            with _locked(self._lock, None):
                index = self._index
                if index is None:
                    index = self._index = {key: i for i, key in enumerate(self.keys())}
        return index

    def row_at(self, position: int) -> Row[FrozenValue]:
        """The row at ``position``, decoding only that row when the rows are not held."""
        if self._rows is not None:
            return self._rows[position]
        from fxtr.entity_defns.arrow import arrow_rows

        assert self._table is not None
        one_row = self._table.slice(position, 1)
        return next(arrow_rows(one_row, self.type))

    def table(self, check_cancelled: Callable[[], None] | None = None) -> pa.Table:
        """The Arrow table, converted from the rows and kept the first time it is asked for."""
        table = self._table
        if table is None:
            from fxtr.entity_defns.arrow import rows_to_arrow_table

            with _locked(self._lock, check_cancelled):
                table = self._table
                if table is None:
                    assert self._rows is not None
                    table = self._table = rows_to_arrow_table(
                        self._rows, self.type, check_cancelled=check_cancelled
                    )
                    self.arrow_conversions += 1
        return table

    def entity_cids(self) -> set[bytes]:
        """The content ID of every entity the array's values hold. An array built from Arrow reads
        its ID columns, not its values."""
        if not contains_entity_ids(self.type.schema):
            return set()
        if self._rows is None:
            from fxtr.entity_defns.arrow import arrow_table_entity_ids

            assert self._table is not None
            return {reference.cid for reference in arrow_table_entity_ids(self._table, self.type)}
        return {
            reference.cid
            for _, value in self._rows
            for reference in iter_entity_ids(cast("StorableFxtrDataValue", value))
        }

    def row_hash(self) -> int:
        """A hash of the rows, equal for equal rows whichever form holds them."""
        if self._hash is None:
            rows = self._rows if self._rows is not None else self.iter_rows()
            value = len(self)
            for chunk in itertools.batched(rows, _HASH_CHUNK):
                value = hash((value, chunk))
            self._hash = value
        return self._hash

    def same_rows(self, other: _Backing) -> bool:
        """Whether the two hold equal rows, by Python value equality."""
        if other is self:
            return True
        if len(self) != len(other):
            return False
        if self._hash is not None and other._hash is not None and self._hash != other._hash:
            return False
        if self._rows is not None and other._rows is not None:
            return self._rows == other._rows
        return all(
            mine == theirs
            for mine, theirs in zip(
                itertools.batched(self.iter_rows(), _HASH_CHUNK),
                itertools.batched(other.iter_rows(), _HASH_CHUNK),
                strict=True,
            )
        )

    def _derive_rows(self) -> tuple[Row[FrozenValue], ...]:
        source = None if self._row_source is None else self._row_source()
        # Once the rows exist there is nothing left to take from the source.
        self._row_source = None
        if source is not None:
            return self._reused_rows(source)
        return tuple(self.iter_rows())

    def _reused_rows(self, source: Array[Any]) -> tuple[Row[FrozenValue], ...]:
        """The rows, as ``source``'s own row objects.

        Rows are immutable, and each of these keys names a row of ``source`` that the query left
        unchanged, so the result can share it: far cheaper than decoding, about fifteen times for
        a filter that keeps half of 200,000 nested-struct rows.
        """
        source_rows = source._backing.rows()
        index = source._backing.index()
        return tuple(source_rows[index[key]] for key in self.keys())


V = TypeVar("V", covariant=True)
"""The Python type of a :class:`Array`'s values. Covariant: an array is immutable, so a
``Array[Literal["x"]]`` is a ``Array[str]``. An old-style TypeVar, since PEP 695 only infers
variance."""


@dataclasses.dataclass(frozen=True, kw_only=True, init=False, eq=False, repr=False)
class Array(DataclassEntity, Generic[V], fxtr_type=ARRAY_TYPE):  # noqa: UP046
    """An immutable sparse named array: unique keys along named dims, each mapped to a value
    of one regular schema.

    The rows (``raw_rows``) are canonical: values in frozen form (see
    :func:`~fxtr.entity_defns.schema.coerce_value`), sorted by key. Equality, hashing, and the
    stored and wire forms are defined by them. An array holds them as Python rows, as an Arrow
    table in the same order, or both: one produced by a query, loaded from storage, or received
    over the wire holds only the table, and decodes rows the first time something reads them.
    Which form an array holds never changes what it equals, hashes to, or is stored as.

    What the array *hands out* is the **presented** form of each value
    (:func:`~fxtr.entity_defns.schema.present_value`): ids whose schema carries a loader come
    back as :class:`~fxtr.core.entities.BoundID`\\ s, structs declared by a TypedDict as dicts,
    lists declared ``list`` as lists, everything else frozen as stored. ``V`` is the Python type
    of the presented values (defaulting to :data:`~fxtr.entity_defns.schema.FrozenValue`), so
    ``Array[BoundID[Bio]]`` and ``Array[SomeTypedDict]`` are honest types; the ``raw_*``
    accessors give the frozen form. Presented values are built afresh on every access, so
    mutating one never affects the array.

    The constructor takes ``raw_rows`` as ``(key, value)`` pairs in any input shape (see
    :data:`~fxtr.entity_defns.schema.ValueInput`) and normalizes them (freezing, validating,
    sorting, rejecting duplicate keys), so any construction path yields the canonical form; the
    usual constructors are :meth:`scalar`, :meth:`from_items`, :meth:`from_records`, and the
    schema-inferring :meth:`infer_scalar`, :meth:`infer_from_items` and
    :meth:`infer_from_records`.

    Read access is mapping-like, keyed by :data:`KeySpec`: ``len``, iteration over keys, ``in``,
    indexing, :meth:`get`, :meth:`keys`, :meth:`values`, :meth:`items`, :meth:`rows` (and
    :attr:`raw_rows`, :meth:`raw_values`, :meth:`raw_items`, :meth:`raw_records`). :meth:`item`
    reads a scalar's single value. These operations return an array at once: :meth:`combine`
    zips arrays into one, :meth:`stack` and :meth:`stack_over` stack arrays along new dims,
    :meth:`transpose` reorders the dims, :meth:`retype` re-declares the type (adopting its
    presentation and dim order), :meth:`expect_type` asserts it, and :meth:`strip_presentation`
    drops presentation. Every other transform, :meth:`sel`, :meth:`rename`, and :meth:`field`
    included, starts a lazy query (:meth:`query`).
    """

    dims: Dims
    schema: Schema
    _backing: _Backing = dataclasses.field(init=False, repr=False, compare=False)
    _presented: bool = dataclasses.field(init=False, repr=False, compare=False)

    # Rows are checked against the schema when an array is built, and decoding stored data back
    # rebuilds the whole Arrow table, so storing skips DataclassEntity's load-back check.
    _fxtr_check_decodes_on_store: ClassVar[bool] = False

    def __init__(
        self, *, dims: DimsSpec, schema: Schema, raw_rows: Iterable[tuple[KeySpec, Any]]
    ) -> None:
        """Build an array, normalizing the rows (see the class docstring).

        Args:
            dims: The dims (any :data:`DimsSpec`).
            schema: The value schema.
            raw_rows: ``(key, value)`` pairs; keys as tuples in dim order, values in any input
                shape for the schema.

        Raises:
            ArrayTypeError: If the dims or schema are malformed.
            ArrayError: If a row is malformed, a value does not fit the schema, two rows share a
                key, or a dim-less array does not have exactly one row.
        """
        ttype = ArrayType(dims=as_dims(dims), schema=schema)  # validates dims and schema
        object.__setattr__(self, "dims", ttype.dims)
        object.__setattr__(self, "schema", ttype.schema)
        normalized: list[Row[FrozenValue]] = []
        for i, row in enumerate(raw_rows):
            if not (isinstance(row, Sequence) and len(row) == 2):
                raise ArrayError(f"row {i}: expected a (key, value) pair, got {row!r}")
            key, value = row
            try:
                frozen = coerce_value(value, self.schema, f"row {i}")
            except ValueSchemaError as exc:
                raise ArrayError(str(exc)) from exc
            normalized.append((self._check_key(key, f"row {i}"), frozen))
        # Dim kinds are int or str, one kind per dim, so keys are totally ordered.
        normalized.sort(key=lambda r: r[0])
        index: dict[Key, int] = {}
        for i, (key, _) in enumerate(normalized):
            if key in index:
                raise ArrayError(f"Duplicate key {dict(self._key_record(key))}")
            index[key] = i
        if not self.dims and len(normalized) != 1:
            raise ArrayError(
                f"An array with no dims must hold exactly one value, got {len(normalized)}"
            )
        self._set_backing(_Backing(ttype, rows=tuple(normalized), index=index))

    def _set_backing(self, backing: _Backing) -> None:
        object.__setattr__(self, "_backing", backing)
        object.__setattr__(self, "_presented", needs_presentation(self.schema))

    @classmethod
    def _with_backing(cls, type: ArrayType, backing: _Backing) -> Array[Any]:
        """An array of ``type`` over ``backing``, whose type must equal ``type``."""
        array = object.__new__(cls)
        object.__setattr__(array, "dims", type.dims)
        object.__setattr__(array, "schema", type.schema)
        array._set_backing(backing)
        return array

    @classmethod
    def _from_arrow(
        cls,
        table: pa.Table,
        type: ArrayType,
        *,
        row_source: Array[Any] | None = None,
    ) -> Array[Any]:
        """An array holding ``table`` and no Python rows, for producers that have validated it.

        ``table`` must be canonical for ``type`` (see
        :func:`~fxtr.entity_defns.arrow.validate_arrow_batches`). ``row_source`` is an input with
        Python rows whose rows these are a subset of (see :class:`_Backing`).
        """
        backing = _Backing(type, table=table, row_source=row_source)
        return cls._with_backing(type, backing)

    def _arrow_table(self, check_cancelled: Callable[[], None] | None = None) -> pa.Table:
        """This array as its canonical Arrow table, converted once and then kept."""
        return self._backing.table(check_cancelled)

    def content_id(self) -> EntityID:
        """The id this array has as a stored entity (see
        :meth:`~fxtr.core.entities.Entity.content_id`), computed once and kept. An array holding
        only its Arrow table computes it from the table, without Python rows.
        """
        backing = self._backing
        if backing.content_id is None:
            if backing.has_rows:
                backing.content_id = super().content_id()
            else:
                from fxtr.entity_defns.arrow import arrow_table_content_id

                backing.content_id = arrow_table_content_id(self._arrow_table(), self.type)
        return backing.content_id

    def entity_cids(self) -> set[bytes]:
        """The content ID of every entity this array's values hold.

        An array holding only its Arrow table reads them from its ID columns, without building
        Python rows.
        """
        return self._backing.entity_cids()

    @property
    def raw_rows(self) -> tuple[Row[FrozenValue], ...]:
        """The canonical rows: ``(key, frozen value)`` pairs, sorted by key.

        An array built from Arrow decodes them the first time, and keeps them. To read the rows
        once, :meth:`raw_items` decodes a batch at a time instead.
        """
        return self._backing.rows()

    # Written out rather than generated: the rows are not a dataclass field. Equality is that of
    # the canonical Python values, whichever form either array holds: ``0.0 == -0.0``.
    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        assert isinstance(other, Array)
        if (self.dims, self.schema) != (other.dims, other.schema):
            return False
        return self._backing.same_rows(other._backing)

    def __hash__(self) -> int:
        return hash((self.dims, self.schema, self._backing.row_hash()))

    def __repr__(self) -> str:
        rows = tuple(self._backing.iter_rows())
        return f"Array(dims={self.dims!r}, schema={self.schema!r}, raw_rows={rows!r})"

    # An array is immutable, so a copy of it is itself.
    def __copy__(self) -> Array[V]:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Array[V]:
        return self

    # Pickling keeps the form the array was built in, and nothing derived from it: a queried
    # Python array does not travel as Arrow, and an unread query result is not decoded to be
    # sent. The weak row source is process-local and is dropped.
    def __getstate__(self) -> dict[str, object]:
        backing = self._backing
        state: dict[str, object] = {"dims": self.dims, "schema": self.schema}
        if backing.source == "rows":
            state["raw_rows"] = backing.rows()
        else:
            state["table"] = backing.table()
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        object.__setattr__(self, "dims", state["dims"])
        object.__setattr__(self, "schema", state["schema"])
        if "raw_rows" in state:
            backing = _Backing(self.type, rows=state["raw_rows"])
        else:
            backing = _Backing(self.type, table=state["table"])
        self._set_backing(backing)

    def _present(self, value: FrozenValue) -> V:
        return cast("V", present_value(value, self.schema) if self._presented else value)

    def _check_key(self, key: object, where: str) -> Key:
        if not isinstance(key, Sequence) or isinstance(key, (str, bytes)):
            raise ArrayError(f"{where}: expected a key tuple, got {type(key).__name__}")
        if len(key) != len(self.dims):
            raise ArrayError(f"{where}: expected {len(self.dims)} key values, got {len(key)}")
        for dim, value in zip(self.dims, key, strict=True):
            if type(value) is not _DIM_PYTHON_TYPES[dim.kind]:
                raise ArrayError(
                    f"{where}: dim {dim.name!r} expects {dim.kind} keys, got {type(value).__name__}"
                )
        return tuple(key)

    # ---- type ----

    @property
    def type(self) -> ArrayType:
        return ArrayType(self.dims, self.schema)

    @property
    def dim_names(self) -> tuple[str, ...]:
        return tuple(dim.name for dim in self.dims)

    def expect_type(self, type: PartialArrayTypeSpec) -> Array[V]:
        """Assert this array's type and return the array, so the assertion can be written inline.

        The dims must match in order, and the value schema must be equal when the spec gives one;
        ``"[model: str, sample: int]"`` names only the dims (see :class:`PartialArrayType`). This
        is documentation that is checked — where a type is *declared* (a step's or workflow's
        signature), declare it there instead. When the spec gives a schema, the returned array
        carries *its* presentation (loaders, TypedDicts, list forms), since schema equality
        ignores presentation: ``array.expect_type(("[id: int]", BoundID[Bio]))`` yields bound ids.

        Raises:
            ArrayTypeError: If the array's type is not the expected one.
        """
        expected = as_partial_array_type(type)
        expected.check(self.type)
        if expected.schema is None:
            return self
        return cast("Array[V]", self.retype(ArrayType(expected.dims, expected.schema)))

    @classmethod
    def _from_validated_rows(
        cls,
        rows: Iterable[Row[FrozenValue]],
        type: ArrayType,
        *,
        canonical_order: bool,
    ) -> Array[Any]:
        """Build an array from rows already proven to conform to ``type``.

        Array operators derive their output type before producing rows, so repeating recursive
        schema coercion would add a full extra traversal. This private constructor retains
        canonical ordering, duplicate-key, and scalar-cardinality checks. Callers remain
        responsible for validating key kinds and values against ``type``.
        """
        normalized = list(rows)
        if not canonical_order:
            normalized.sort(key=lambda row: row[0])
        index: dict[Key, int] = {}
        previous: Key | None = None
        for position, (key, _) in enumerate(normalized):
            if canonical_order and previous is not None and key < previous:
                raise ArrayError("Validated rows declared canonical are not sorted by key")
            if key in index:
                raise ArrayError(f"Duplicate key {dict(zip(type.dim_names, key, strict=True))}")
            index[key] = position
            previous = key
        if not type.dims and len(normalized) != 1:
            raise ArrayError(
                f"An array with no dims must hold exactly one value, got {len(normalized)}"
            )

        return cls._with_backing(type, _Backing(type, rows=tuple(normalized), index=index))

    def strip_presentation(self) -> Array[Any]:
        """This array with presentation removed from its schema (see
        :func:`~fxtr.entity_defns.schema.strip_presentation`); the frozen rows are unchanged, and
        so are equality, hashing, and the content id. Unchanged if there is no presentation.

        This is the form arrays take across the worker/client boundary, so a backend never holds a
        presented value: the raw and presented accessors coincide (see :attr:`raw_rows`).
        """
        schema = strip_presentation(self.schema)
        if schema is self.schema:
            return self
        return Array._with_backing(ArrayType(self.dims, schema), self._backing)

    def retype(self, type: ArrayTypeSpec) -> Array[Any]:
        """Re-express this array with the given type and presentation.

        Requires the original type to be assignable to the new one. The presentation-level
        conversion (e.g. the Python type of bound IDs) is taken from the new type.

        This can be used to check or refine a type, or to attach or replace a type annotation.
        Retyping to an equal type, which differs at most in presentation, converts nothing: the
        result shares this array's rows and table.

        A looser declaration (an untyped ``id`` over a typed one, a nullable schema
        over a non-nullable one) is adopted as declared.

        Raises:
            ArrayTypeError: If this array's type is not assignable to ``type``.
        """
        target = as_array_type(type)
        if not is_assignable_array(self.type, target):
            raise ArrayTypeError(f"Cannot retype an array of type {self.type} as {target}")
        if target == self.type:
            return Array._with_backing(target, self._backing)
        from fxtr.entity_defns.arrow import retype_arrow_table

        return Array._from_arrow(retype_arrow_table(self._arrow_table(), self.type, target), target)

    # ---- construction ----

    # The builders' value parameter, like the constructor's, is deliberately unbounded. A
    # ``ValueInput`` bound would reject a ``TypedDict``, which is consistent with
    # ``Mapping[str, object]`` and nothing narrower and so never fits the recursive
    # ``Mapping[str, ValueInput]`` member, even though a TypedDict is the ordinary way to write a
    # struct value. What a value may be is enforced where every other value's shape is: against
    # the schema, when it is coerced.

    @classmethod
    def scalar[T](cls, value: T, schema: SchemaSpec) -> Array[T]:
        """An array with no dims holding ``value``."""
        return Array[T](dims=(), schema=as_schema(schema), raw_rows=(((), value),))

    @classmethod
    def infer_scalar[T](cls, value: T) -> Array[T]:
        """An array with no dims holding ``value``, with the schema inferred (see
        :func:`~fxtr.entity_defns.schema.infer_schema`).

        Raises:
            ArrayError: If a schema cannot be inferred.
        """
        try:
            schema = infer_schema(value)
        except ValueError as exc:
            raise ArrayError(str(exc)) from exc
        return Array[T](dims=(), schema=schema, raw_rows=(((), value),))

    @staticmethod
    def _key_tuples(items: Iterable[tuple[KeySpec, Any]], names: Sequence[str]) -> list[Row[Any]]:
        rows: list[Row[Any]] = []
        for i, (key, value) in enumerate(items):
            if isinstance(key, Mapping):
                if set(key) != set(names):
                    raise ArrayError(f"item {i}: expected dims {list(names)}, got {sorted(key)}")
                key = tuple(key[n] for n in names)
            rows.append((tuple(key), value))
        return rows

    @classmethod
    def from_items[T](cls, items: Iterable[tuple[KeySpec, T]], type: ArrayTypeSpec) -> Array[T]:
        """Build an array of the given type from ``(key, value)`` pairs, keys given as tuples or
        mappings.

        Raises:
            ArrayError: If a key or value is invalid or two items share a key.
        """
        ttype = as_array_type(type)
        rows = cls._key_tuples(items, ttype.dim_names)
        return Array[T](dims=ttype.dims, schema=ttype.schema, raw_rows=tuple(rows))

    @classmethod
    def infer_from_items[T](
        cls, items: Iterable[tuple[KeySpec, T]], dims: Sequence[str] | None = None
    ) -> Array[T]:
        """Build an array from ``(key, value)`` pairs, inferring dim kinds and the value schema
        (see :func:`~fxtr.entity_defns.schema.infer_schema`).

        Args:
            items: The rows. At least one is required.
            dims: The dim names, in order. May be omitted when keys are mappings, in which case
                the first key's order is used.

        Raises:
            ArrayError: If there are no items, ``dims`` is omitted for tuple keys, keys do not
                match the dims, or a schema cannot be inferred.
            ArrayTypeError: If a dim's inferred kind is not a key kind.
        """
        materialized = list(items)
        if not materialized:
            raise ArrayError("Cannot infer an array from zero items; give an explicit schema")
        if dims is None:
            first = materialized[0][0]
            if not isinstance(first, Mapping):
                raise ArrayError("dims are required when keys are given as tuples")
            dims = tuple(first)
        rows = cls._key_tuples(materialized, dims)
        for i, (key, _) in enumerate(rows):
            if len(key) != len(dims):
                raise ArrayError(f"item {i}: expected {len(dims)} key values, got {len(key)}")
        dims_ = tuple(
            Dim(d, _key_kind(_infer_common([k[i] for k, _ in rows], f"dim {d!r}"), f"Dim {d!r}"))
            for i, d in enumerate(dims)
        )
        schema = _infer_common([v for _, v in rows], "value")
        return Array[T](dims=dims_, schema=schema, raw_rows=tuple(rows))

    @classmethod
    def from_records(
        cls, records: Iterable[Mapping[str, Any]], type: ArrayTypeSpec
    ) -> Array[frozendict[str, FrozenValue]]:
        """Build a struct-valued array of the given type from flat records holding both dim keys
        and struct fields.

        Each record must have exactly the dim names plus the struct's field names as keys.

        Raises:
            ArrayTypeError: If the type's schema is not a struct.
            ArrayError: If a record's keys do not match, a value is invalid, or two records
                share a key.
        """
        ttype = as_array_type(type)
        dims_, schema_ = ttype.dims, ttype.schema
        if not isinstance(schema_, StructSchema):
            raise ArrayTypeError(f"from_records needs a struct schema, got {schema_}")
        names = list(ttype.dim_names)
        expected = set(names) | set(schema_.fields)
        rows: list[tuple[Any, Any]] = []
        for i, record in enumerate(records):
            if set(record) != expected:
                raise ArrayError(
                    f"record {i}: expected keys {sorted(expected)}, got {sorted(record)}"
                )
            rows.append((tuple(record[n] for n in names), {f: record[f] for f in schema_.fields}))
        return Array[frozendict[str, FrozenValue]](dims=dims_, schema=schema_, raw_rows=tuple(rows))

    @classmethod
    def infer_from_records(
        cls, records: Iterable[Mapping[str, Any]], dims: Sequence[str]
    ) -> Array[frozendict[str, FrozenValue]]:
        """Build a struct-valued array from flat records, inferring dim kinds and the struct schema
        (see :func:`~fxtr.entity_defns.schema.infer_schema`).

        Args:
            records: The rows, as mappings with identical keys. At least one is required.
            dims: The names of the dim keys among the record keys, in order.

        Raises:
            ArrayError: If there are no records, records disagree on keys, a dim is not present,
                or a schema cannot be inferred.
            ArrayTypeError: If a dim's inferred kind is not a key kind.
        """
        materialized = [dict(r) for r in records]
        if not materialized:
            raise ArrayError("Cannot infer an array from zero records; give an explicit schema")
        names = tuple(materialized[0])
        for i, record in enumerate(materialized):
            if set(record) != set(names):
                raise ArrayError(f"record {i}: expected keys {sorted(names)}, got {sorted(record)}")
        missing = [d for d in dims if d not in names]
        if missing:
            raise ArrayError(f"Dims {missing} are not present in the records")
        dims_ = tuple(
            Dim(d, _key_kind(_infer_column(materialized, d, f"dim {d!r}"), f"Dim {d!r}"))
            for d in dims
        )
        schema = StructSchema(
            frozendict(
                {f: _infer_column(materialized, f, f"field {f!r}") for f in names if f not in dims}
            )
        )
        return cls.from_records(materialized, (dims_, schema))

    # ---- mapping-style access ----

    def _key_tuple(self, key: KeySpec) -> Key:
        names = self.dim_names
        if isinstance(key, Mapping):
            if set(key) != set(names):
                raise KeyError(f"Expected dims {list(names)}, got {sorted(key)}")
            return tuple(key[n] for n in names)
        if len(key) != len(names):
            raise KeyError(f"Expected {len(names)} key values, got {len(key)}")
        return tuple(key)

    def _key_record(self, key: Key) -> frozendict[str, KeyValue]:
        return frozendict(zip(self.dim_names, key, strict=True))

    def __len__(self) -> int:
        return len(self._backing)

    def __iter__(self) -> Iterator[Key]:
        return self.keys()

    def __contains__(self, key: KeySpec) -> bool:
        try:
            return self._key_tuple(key) in self._backing.index()
        except KeyError:
            return False

    def _raw_at(self, key: Key) -> FrozenValue:
        """The frozen value at an exact key tuple (``KeyError`` if absent)."""
        return self._backing.row_at(self._backing.index()[key])[1]

    def row_position(self, key: Key) -> int:
        """The position in :attr:`raw_rows` of the row with exactly the key tuple ``key``.

        Rows are held in key order, so positions increase with keys.

        Raises:
            KeyError: If there is no such row.
        """
        return self._backing.index()[key]

    def __getitem__(self, key: KeySpec) -> V:
        """The (presented) value at ``key`` (a tuple in dim order, or a name-to-value mapping).

        Raises:
            KeyError: If the key names the wrong dims or there is no such row.
        """
        key_tuple = self._key_tuple(key)
        position = self._backing.index().get(key_tuple)
        if position is None:
            raise KeyError(dict(self._key_record(key_tuple)))
        return self._present(self._backing.row_at(position)[1])

    @overload
    def get(self, key: KeySpec) -> V | None: ...
    @overload
    def get[T](self, key: KeySpec, default: T) -> V | T: ...
    def get(self, key: KeySpec, default: object = None) -> object:
        """The (presented) value at ``key``, or ``default`` if there is no such row."""
        try:
            return self[key]
        except KeyError:
            return default

    # The iterators below read the rows in order without asking for all of them at once: an
    # array built from Arrow decodes a batch at a time and keeps none.

    def keys(self) -> Iterator[Key]:
        """The keys, in key order."""
        return self._backing.keys()

    def key_records(self) -> Iterator[frozendict[str, KeyValue]]:
        """The keys as name-to-value mappings, in key order."""
        return (self._key_record(key) for key in self._backing.keys())

    @property
    def rows(self) -> tuple[Row[V], ...]:
        """The rows as ``(key, presented value)`` pairs, in key order (see :attr:`raw_rows` for
        the frozen values)."""
        if not self._presented:
            return cast("tuple[Row[V], ...]", self.raw_rows)
        return tuple((key, self._present(value)) for key, value in self._backing.iter_rows())

    def values(self) -> Iterator[V]:
        """The presented values, in key order."""
        return (self._present(value) for _, value in self._backing.iter_rows())

    def items(self) -> Iterator[Row[V]]:
        """``(key, presented value)`` pairs, in key order."""
        return ((key, self._present(value)) for key, value in self._backing.iter_rows())

    def raw_values(self) -> Iterator[FrozenValue]:
        """The frozen values, in key order."""
        return (value for _, value in self._backing.iter_rows())

    def raw_items(self) -> Iterator[Row[FrozenValue]]:
        """``(key, frozen value)`` pairs, in key order."""
        return self._backing.iter_rows()

    def records(self) -> Iterator[frozendict[str, Any]]:
        """Flat records (dim keys plus struct fields), in key order, for a struct-valued array.

        Field values are presented; the record itself is a frozendict, since with the dim keys
        mixed in it is not the struct's TypedDict.

        Raises:
            ArrayTypeError: If the value schema is not a (non-nullable) struct.
        """
        if not isinstance(self.schema, StructSchema):
            raise ArrayTypeError(f"records() needs a struct-valued array, got {self.schema}")
        return (
            frozendict({**self._key_record(key), **self._present(value)})  # type: ignore[dict-item]
            for key, value in self._backing.iter_rows()
        )

    def raw_records(self) -> Iterator[frozendict[str, FrozenValue]]:
        """Flat records with frozen field values (see :meth:`records`).

        Raises:
            ArrayTypeError: If the value schema is not a (non-nullable) struct.
        """
        if not isinstance(self.schema, StructSchema):
            raise ArrayTypeError(f"raw_records() needs a struct-valued array, got {self.schema}")
        return (
            frozendict({**self._key_record(key), **value})  # type: ignore[dict-item]
            for key, value in self._backing.iter_rows()
        )

    def item(self) -> V:
        """The single (presented) value of an array with no dims.

        Raises:
            ArrayTypeError: If the array has dims.
        """
        if self.dims:
            raise ArrayTypeError(
                f"item() needs an array with no dims, got dims {list(self.dim_names)}"
            )
        return self._present(self._backing.row_at(0)[1])

    # ---- operations ----
    # The immediate operations (retype above; transpose here; stack_over, stack and combine after
    # the lazy queries) compute their result from the Arrow tables, so an operation on a query
    # result decodes nothing and its result holds only a table too.

    def transpose(self, names: Sequence[str]) -> Array[V]:
        """This array with its dims reordered to ``names`` (see :func:`transpose_type`). Rows are
        re-sorted under the new key order.

        Raises:
            ArrayTypeError: If ``names`` is not a permutation of the dim names.
        """
        out = transpose_type(self.type, names)
        from fxtr.entity_defns.arrow import retype_arrow_table

        return Array._from_arrow(retype_arrow_table(self._arrow_table(), self.type, out), out)

    # ---- lazy queries ----
    # The methods from here to ``join`` only describe a query: each returns an ArrayQuery, and
    # nothing runs until that is awaited with ``collect()`` (an Array) or ``item()`` (a scalar's
    # value).

    def query(
        self,
        *,
        limits: DuckDBExecutionLimits | None = None,
        query_limiter: anyio.CapacityLimiter | None = None,
    ) -> ArrayQuery[V]:
        """Build a lazy local DuckDB query; only ``collect()`` or ``item()`` executes it.

        The shortcuts below (:meth:`filter`, :meth:`select`, ...) call this with no arguments.
        Start from here instead to set ``limits`` or a shared ``query_limiter``. Without them
        DuckDB's own memory and thread defaults apply and fxtr adds no result-size cap,
        deadline, or concurrency cap.

        Arrow conversion is cached on this immutable array. Querying neither stores the array
        nor computes its CBOR content ID.
        """
        from fxtr.duckdb_engine.local_query import query_array

        return query_array(self, limits=limits, query_limiter=query_limiter)

    def filter(self, predicate: Condition) -> ArrayQuery[V]:
        """Lazily keep the rows where ``predicate`` is true; see :meth:`ArrayQuery.filter`.

        To take the rows at given dim values and drop those dims, use :meth:`sel`.
        """
        return self.query().filter(predicate)

    def field(self, name: str) -> ArrayQuery[Any]:
        """Lazily replace each value with its struct field ``name``; see
        :meth:`ArrayQuery.field`."""
        return self.query().field(name)

    def sel(self, key: Mapping[str, KeyValue]) -> ArrayQuery[V]:
        """Lazily keep the rows at the given dim values and drop those dims; see
        :meth:`ArrayQuery.sel`."""
        return self.query().sel(key)

    def rename(self, names: Mapping[str, str]) -> ArrayQuery[V]:
        """Lazily rename dims, old name to new name; see :meth:`ArrayQuery.rename`."""
        return self.query().rename(names)

    def select(self, fields: Mapping[str, ValueExpression] | ValueExpression) -> ArrayQuery[Any]:
        """Lazily replace each row value with one expression, or with a struct of named ones;
        see :meth:`ArrayQuery.select`.

        This computes values; it is unrelated to :meth:`sel`, which slices along dims. To take
        one struct field, use :meth:`field`.
        """
        return self.query().select(fields)

    def limit(
        self,
        count: int,
        *,
        order_by: OrderBy = (),
        ties: TiePolicy = "first",
    ) -> ArrayQuery[V]:
        """Lazily keep the first ``count`` rows under ``order_by``; see
        :meth:`ArrayQuery.limit`."""
        return self.query().limit(count, order_by=order_by, ties=ties)

    def aggregate(
        self,
        aggregations: Mapping[str, Aggregation],
        *,
        group_by: Mapping[str, ValueExpression] = frozendict(),
    ) -> ArrayQuery[Any]:
        """Lazily aggregate all rows, or each ``group_by`` group; see
        :meth:`ArrayQuery.aggregate`."""
        return self.query().aggregate(aggregations, group_by=group_by)

    def join(
        self,
        other: ArrayQuery[Any] | Array[Any],
        *,
        on: JoinOn,
        left_name: str = "left",
        right_name: str = "right",
    ) -> ArrayQuery[Any]:
        """Lazily inner-join with another array or local query on equal columns; see
        :meth:`ArrayQuery.join`.

        To zip arrays that share dims into one struct value, :meth:`combine` returns an array at
        once.
        """
        return self.query().join(other, on=on, left_name=left_name, right_name=right_name)

    @classmethod
    def stack_over[T](cls, arrays: Mapping[Key, Array[T]], dims: Dims) -> Array[T]:
        """Stack same-typed arrays along new leading dims ``dims``, each array at the key it is
        mapped from (a tuple in ``dims`` order; see :func:`stack_over_type`).

        Raises:
            ArrayTypeError: If the arrays differ in type, or a stacked dim collides with an
                existing dim.
            ArrayError: If a key does not have one value of the right kind per stacked dim.
        """
        out = stack_over_type((t.type for t in arrays.values()), dims)
        for i, key in enumerate(arrays):
            if len(key) != len(dims):
                raise ArrayError(f"key {i}: expected {len(dims)} key values, got {len(key)}")
            for dim, value in zip(dims, key, strict=True):
                if type(value) is not _DIM_PYTHON_TYPES[dim.kind]:
                    raise ArrayError(
                        f"key {i}: dim {dim.name!r} expects {dim.kind} keys, got"
                        f" {type(value).__name__}"
                    )
        from fxtr.entity_defns.arrow import stack_arrow_tables

        parts = [(key, array._arrow_table(), array.type) for key, array in arrays.items()]
        return cls._from_arrow(stack_arrow_tables(parts, out), out)

    @classmethod
    def stack[T](cls, arrays: Mapping[KeyValue, Array[T]], dim: str) -> Array[T]:
        """Stack same-typed arrays along a new leading dim, keyed by the mapping's keys: the
        one-dim case of :meth:`stack_over`.

        The new dim's kind is that of the keys, which must all be ``int`` or all be ``str`` (see
        :func:`stack_dim`).

        Raises:
            ArrayTypeError: If the arrays differ in type, the keys are not all of one key kind, or
                ``dim`` collides with an existing dim.
        """
        return cls.stack_over({(k,): t for k, t in arrays.items()}, (stack_dim(arrays, dim),))

    @classmethod
    def combine(
        cls, inputs: Mapping[str, Array[Any]], join: CombineJoin = "exact"
    ) -> Array[frozendict[str, FrozenValue]]:
        """Zip arrays into one struct-valued array with a field per input (see
        :func:`combine_type` and :func:`join_keys`).

        The result's dims are the union of the inputs' dims; each input is broadcast along the
        dims it lacks.

        Raises:
            ArrayTypeError: If inputs disagree on a dim's kind.
            ArrayError: If ``join="exact"`` and key sets disagree.
        """
        out = combine_type({name: t.type for name, t in inputs.items()}, join)
        keys = join_keys(inputs, out.dims, join)
        names = out.dim_names
        pickers = {name: [names.index(d) for d in t.dim_names] for name, t in inputs.items()}

        from fxtr.entity_defns.arrow import combine_arrow_tables

        columns: dict[str, tuple[pa.Table, ArrayType, list[int | None]]] = {}
        for name, array in inputs.items():
            index = array._backing.index()
            positions = [index.get(tuple(key[i] for i in pickers[name])) for key in keys]
            columns[name] = (array._arrow_table(), array.type, positions)
        return cls._from_arrow(combine_arrow_tables(columns, keys, out), out)

    # ---- serialization hooks (see fxtr.entity_defns.dataclass_bases) ----

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        def hook(array: Array[Any]) -> dict[str, Any]:
            return _array_to_wire(array)

        return hook

    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        # ``Array[X]`` with a concrete ``X`` says what the loaded array's values should present
        # as: the stored schema is checked to be assignable to X's and then X's is adopted.
        annotated = _annotated_schema(annotation)

        def hook(value: Any, _target: Any) -> Array[Any]:
            if not isinstance(value, Mapping) or set(value) != {"dims", "schema", "rows"}:
                raise StrictTypeError(
                    "Array data must have exactly 'dims', 'schema' and 'rows' (plus the type"
                    f" tag), got"
                    f" {sorted(value) if isinstance(value, Mapping) else type(value).__name__}"
                )
            data = cast("Mapping[str, Any]", value)
            array = _decoded_into_arrow(data)
            if array is None:
                array = _structured_into_rows(data)
            if annotated is not None:
                if not is_assignable(array.schema, annotated):
                    raise StrictTypeError(
                        f"Array data has schema {array.schema}, which is not assignable to the"
                        f" annotated {annotated}"
                    )
                array = array.retype(ArrayType(array.dims, annotated))
            return array

        return hook


def _decoded_into_arrow(value: Mapping[str, Any]) -> Array[Any] | None:
    """The array ``value`` (an array's data, its type tag removed) describes, holding an Arrow
    table built straight from it; ``None`` if the decoder refuses it, and
    :func:`_structured_into_rows` then accepts or refuses it."""
    from fxtr.entity_defns.arrow import array_from_canonical_data

    try:
        return array_from_canonical_data({TYPE_KEY: ARRAY_TYPE, **value}, None)
    except ArrayError:
        return None


def _structured_into_rows(value: Mapping[str, Any]) -> Array[Any]:
    """The array ``value`` describes, holding Python rows.

    Raises:
        StrictTypeError: If the rows are malformed or not in canonical (key-sorted) order.
    """
    ttype = ArrayType.from_wire({"dims": value["dims"], "schema": value["schema"]})
    raw_rows = value["rows"]
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise StrictTypeError(f"Array rows must be a list, got {type(raw_rows).__name__}")
    n = len(ttype.dims)
    rows: list[tuple[Any, Any]] = []
    for i, raw in enumerate(cast("Sequence[Any]", raw_rows)):
        if not (isinstance(raw, Sequence) and len(raw) == n + 1):
            raise StrictTypeError(f"row {i}: expected {n} keys and a value, got {raw!r}")
        rows.append((tuple(raw[:n]), raw[n]))
    array = Array(dims=ttype.dims, schema=ttype.schema, raw_rows=tuple(rows))
    # Distinct wire forms must not alias to one array: the stored rows must already be in
    # canonical (key-sorted) order, so re-serializing reproduces the input exactly.
    if _array_to_wire(array) != value:
        raise StrictTypeError("Array data is not in canonical form (rows sorted by key)")
    return array


type ScalarArray[X] = Annotated[Array[X], "[]"]
"""A scalar (no-dims) array annotation: ``ScalarArray[X]`` stands for ``Annotated[Array[X], "[]"]``,
the dims-less array annotation spelled once (the annotation reader expands the alias; see
:func:`~fxtr.entity_defns.schema.expand_type_alias`). Use it in a step or workflow function's
signature, e.g. ``model: ScalarArray[EntityID]``."""


def _annotated_schema(annotation: Any) -> Schema | None:
    """The value schema an ``Array[X]`` annotation asks for, or ``None`` when ``X`` is absent or
    says nothing (a type variable, ``Any``, or the :data:`~fxtr.entity_defns.schema.FrozenValue`
    default).

    Raises:
        StrictTypeError: If ``X`` is given but is not a schema annotation.
    """
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) is not Array or not args:
        return None
    arg = args[0]
    if isinstance(arg, TypeVar) or arg is Any or arg is FrozenValue:
        return None
    try:
        return schema_from_annotation(arg)
    except SchemaError as exc:
        raise StrictTypeError(f"Array[...] annotation: {exc}") from exc


def _array_to_wire(array: Array[Any]) -> dict[str, Any]:
    return {
        "dims": _dims_to_wire(array.dims),
        "schema": schema_to_wire(array.schema),
        "rows": [[*key, value_to_wire(value)] for key, value in array.raw_rows],
    }
