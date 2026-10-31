"""Canonical Apache Arrow representations of :mod:`fxtr.entity_defns.array` values.

Arrow is a derived physical representation.  The logical identity of an array remains its
canonical DAG-CBOR entity, so this module fixes one versioned mapping from :class:`ArrayType` to
an Arrow schema and can encode an Arrow table into exactly the bytes used by :class:`Array`.

Top-level struct values use one Arrow column per struct field.  Other value schemas use one
``value`` column.  Dimension columns always precede value columns.  Physical names are made
unique when a dimension and value field share a name; the fxtr type and each column's logical
role/name live in schema metadata, so physical naming never changes the logical array.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import itertools
import math
import struct
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Literal, cast

import pyarrow as pa
import pyarrow.compute as pc
from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entity_data import TYPE_KEY, EntityID, FxtrDataValue
from fxtr.entity_defns.array import (
    ARRAY_TYPE,
    Array,
    ArrayError,
    ArrayType,
    Key,
    Row,
)
from fxtr.entity_defns.schema import (
    LEAF_PYTHON_TYPES,
    FrozenValue,
    IdSchema,
    LeafSchema,
    ListSchema,
    NullableSchema,
    Schema,
    StructSchema,
    contains_entity_ids,
    schema_to_wire,
)

ARROW_SCHEMA_VERSION = 3
"""Version of the fxtr-to-Arrow schema contract.

An Arrow table carries its logical array type and its column roles in schema metadata, and
nothing about which array it holds. Physical column names are distinct ignoring case. Empty
structs use a false boolean rather than an Arrow struct with no fields, which DuckDB cannot
read. Record-batch boundaries are not part of compatibility or canonical identity.
"""

DEFAULT_ARROW_BATCH_SIZE = 65_536
"""Rows per Arrow record batch, both when an array is converted to Arrow and when DuckDB results
are read."""

_FORMAT_METADATA = b"fxtr:arrow-format-version"
_ARRAY_TYPE_METADATA = b"fxtr:array-type"
_COLUMN_ROLE_METADATA = b"fxtr:column-role"
_COLUMN_NAME_METADATA = b"fxtr:logical-name"
_COLUMN_SCHEMA_METADATA = b"fxtr:value-schema"

# PyArrow registers compute kernels dynamically; its type information does not expose these
# otherwise stable public functions. Keep the untyped lookup at this boundary and give the calls
# precise signatures throughout the validation code.
_BINARY_ARRAY_KERNEL = Callable[[pa.Array, pa.Array], pa.Array]
_pc_equal = cast(_BINARY_ARRAY_KERNEL, getattr(pc, "equal"))
_pc_less = cast(_BINARY_ARRAY_KERNEL, getattr(pc, "less"))
_pc_and = cast(_BINARY_ARRAY_KERNEL, getattr(pc, "and_"))
_pc_or = cast(_BINARY_ARRAY_KERNEL, getattr(pc, "or_"))
_pc_any = cast(Callable[[pa.Array], pa.Scalar], getattr(pc, "any"))
_pc_is_finite = cast(Callable[[pa.Array], pa.Array], getattr(pc, "is_finite"))
_pc_invert = cast(Callable[[pa.Array], pa.Array], getattr(pc, "invert"))
_pc_is_in = cast(_BINARY_ARRAY_KERNEL, getattr(pc, "is_in"))


def cancellable[T](
    items: Iterable[T], check_cancelled: Callable[[], None] | None, *, every: int = 128
) -> Iterator[T]:
    """Yield ``items``, calling ``check_cancelled`` before the first and then every ``every``.

    These conversions run in worker threads, which AnyIO cannot interrupt. Checking as they go
    lets a cancelled or timed-out query stop promptly, without the cost of a check per row.
    """
    for index, item in enumerate(items):
        if check_cancelled is not None and index % every == 0:
            check_cancelled()
        yield item


class ArrowArrayError(ArrayError):
    """An Arrow object cannot represent the declared fxtr array without changing its value.

    A query result is checked in its Arrow form, so this is also what a query raises when its
    rows do not make a valid array: a repeated key, a null dimension, or a result with no
    dimensions that is not exactly one row.
    """


def arrow_table_entity_ids(
    table: pa.Table,
    array_type: ArrayType,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[EntityID, ...]:
    """Return distinct visible references, without decoding ordinary values or hidden children."""
    references: dict[bytes, EntityID] = {}
    for batch in table.to_batches(max_chunksize=DEFAULT_ARROW_BATCH_SIZE):
        if check_cancelled is not None:
            check_cancelled()
        for _, array in arrow_entity_id_columns(batch, array_type):
            for cid in cancellable(
                cast("list[bytes]", array.unique().to_pylist()), check_cancelled
            ):
                _require_arrow_cid(cid)
                references.setdefault(cid, EntityID(cid=cid))
    return tuple(references.values())


def arrow_entity_id_columns(
    batch: pa.RecordBatch, array_type: ArrayType
) -> Iterator[tuple[IdSchema, pa.Array]]:
    """Yield visible ID columns, ignoring child slots hidden by nullable parents."""
    for index, column in enumerate(arrow_columns(array_type)):
        if column.role == "value" and contains_entity_ids(column.schema):
            yield from _nested_arrow_id_columns(batch.column(index), column.schema)


def _nested_arrow_id_columns(
    array: pa.Array, schema: Schema
) -> Iterator[tuple[IdSchema, pa.Array]]:
    if isinstance(schema, NullableSchema):
        yield from _nested_arrow_id_columns(array.drop_null(), schema.inner)
        return
    if array.null_count:
        raise ArrowArrayError(f"Non-nullable schema {schema} contains a null")
    match schema:
        case IdSchema():
            yield schema, array
        case ListSchema(element):
            assert isinstance(array, pa.ListArray)
            yield from _nested_arrow_id_columns(array.flatten(), element)
        case StructSchema(fields):
            assert isinstance(array, pa.StructArray)
            for name, field in fields.items():
                if contains_entity_ids(field):
                    yield from _nested_arrow_id_columns(array.field(name), field)


def _require_arrow_cid(cid: bytes) -> None:
    if len(cid) != 36:
        raise ArrowArrayError(f"Entity ID must contain a 36-byte CID, got {len(cid)}")
    if not cid.startswith(b"\x01\x71\x12\x20"):
        raise ArrowArrayError("Entity ID must be a DAG-CBOR SHA-256 CID")


@dataclasses.dataclass(frozen=True)
class ArrowColumn:
    """One physical Arrow column and the logical array component it stores."""

    physical_name: str
    role: Literal["dimension", "value"]
    logical_name: str | None
    schema: Schema


# The Arrow layout of a type is asked for on every conversion and operation, and building it
# again each time was most of what a small array's Arrow work cost. Types are immutable, equal
# types have the same layout whatever their presentation, and the results are immutable, so they
# are computed once per type. The bound keeps a program that makes many types from growing.
_LAYOUT_CACHE_SIZE = 4096


@functools.lru_cache(maxsize=_LAYOUT_CACHE_SIZE)
def arrow_columns(array_type: ArrayType) -> tuple[ArrowColumn, ...]:
    """Return the deterministic physical columns for ``array_type`` in storage order."""
    # DuckDB resolves column names without regard to case, so a dimension ``i`` and a value
    # field ``I`` need distinct physical names even though fxtr treats them as different names.
    used: set[str] = set()

    def unique_name(preferred: str, fallback: str) -> str:
        suffix = 0
        candidate = preferred
        while candidate.casefold() in used:
            candidate = fallback if suffix == 0 else f"{fallback}_{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        return candidate

    columns: list[ArrowColumn] = []
    for index, dim in enumerate(array_type.dims):
        columns.append(
            ArrowColumn(
                physical_name=unique_name(dim.name, f"__fxtr_dim_{index}"),
                role="dimension",
                logical_name=dim.name,
                schema=LeafSchema(dim.kind),
            )
        )

    if isinstance(array_type.schema, StructSchema) and array_type.schema.fields:
        for index, name in enumerate(array_type.schema.names):
            columns.append(
                ArrowColumn(
                    physical_name=unique_name(name, f"__fxtr_value_{index}"),
                    role="value",
                    logical_name=name,
                    schema=array_type.schema.fields[name],
                )
            )
    else:
        columns.append(
            ArrowColumn(
                physical_name=unique_name("value", "__fxtr_value"),
                role="value",
                logical_name=None,
                schema=array_type.schema,
            )
        )
    return tuple(columns)


def _schema_field(
    name: str,
    schema: Schema,
    *,
    role: Literal["dimension", "value"],
    logical_name: str | None,
) -> pa.Field:
    nullable = isinstance(schema, NullableSchema)
    inner = schema.inner if nullable else schema
    metadata = {
        _COLUMN_ROLE_METADATA: role.encode(),
        _COLUMN_NAME_METADATA: b"" if logical_name is None else logical_name.encode(),
        _COLUMN_SCHEMA_METADATA: codec.encode(schema_to_wire(schema)),
    }
    return pa.field(
        name,
        _arrow_data_type(inner),
        nullable=nullable,
        metadata=metadata,
    )


@functools.lru_cache(maxsize=_LAYOUT_CACHE_SIZE)
def _arrow_data_type(schema: Schema) -> pa.DataType:
    match schema:
        case NullableSchema(inner):
            return _arrow_data_type(inner)
        case IdSchema():
            return pa.binary(36)
        case LeafSchema("int"):
            return pa.int64()
        case LeafSchema("float"):
            return pa.float64()
        case LeafSchema("bool"):
            return pa.bool_()
        case LeafSchema("str"):
            return pa.string()
        case LeafSchema("bytes"):
            return pa.binary()
        case ListSchema(element):
            nullable = isinstance(element, NullableSchema)
            inner = element.inner if nullable else element
            element_metadata = {_COLUMN_SCHEMA_METADATA: codec.encode(schema_to_wire(element))}
            return pa.list_(
                pa.field(
                    "item",
                    _arrow_data_type(inner),
                    nullable=nullable,
                    metadata=element_metadata,
                )
            )
        case StructSchema(fields):
            if not fields:
                return pa.bool_()
            return pa.struct(
                [
                    _schema_field(
                        name,
                        fields[name],
                        role="value",
                        logical_name=name,
                    )
                    for name in sorted(fields)
                ]
            )


@functools.lru_cache(maxsize=_LAYOUT_CACHE_SIZE)
def array_arrow_schema(array_type: ArrayType) -> pa.Schema:
    """Map an :class:`ArrayType` to its versioned canonical Arrow schema."""
    fields = [
        _schema_field(
            column.physical_name,
            column.schema,
            role=column.role,
            logical_name=column.logical_name,
        )
        for column in arrow_columns(array_type)
    ]
    metadata = {
        _FORMAT_METADATA: str(ARROW_SCHEMA_VERSION).encode(),
        _ARRAY_TYPE_METADATA: codec.encode(array_type.to_wire()),
    }
    return pa.schema(fields, metadata=metadata)


def array_type_from_arrow_schema(schema: pa.Schema) -> ArrayType:
    """Read and validate the fxtr type embedded in an Arrow schema."""
    metadata = schema.metadata or {}
    expected_version = str(ARROW_SCHEMA_VERSION).encode()
    if metadata.get(_FORMAT_METADATA) != expected_version:
        raise ArrowArrayError("Arrow schema has an incompatible format version")
    encoded_type = metadata.get(_ARRAY_TYPE_METADATA)
    if encoded_type is None:
        raise ArrowArrayError("Arrow schema is missing its fxtr array type")
    try:
        array_type = ArrayType.from_wire(codec.decode(encoded_type))
    except Exception as exc:
        raise ArrowArrayError("Arrow schema has invalid fxtr array type metadata") from exc
    expected = array_arrow_schema(array_type)
    if not schema.equals(expected, check_metadata=True):
        raise ArrowArrayError("Arrow schema does not match its fxtr array type metadata")
    return array_type


def require_arrow_schema(schema: pa.Schema, array_type: ArrayType) -> None:
    """Raise unless ``schema`` is exactly the canonical Arrow schema for ``array_type``."""
    expected = array_arrow_schema(array_type)
    if not schema.equals(expected, check_metadata=True):
        raise ArrowArrayError(f"Expected Arrow schema {expected}, got {schema}")


def array_to_arrow_batches(
    array: Array[object],
    *,
    batch_size: int = DEFAULT_ARROW_BATCH_SIZE,
    check_cancelled: Callable[[], None] | None = None,
) -> Iterator[pa.RecordBatch]:
    """An :class:`Array` as bounded canonical Arrow record batches.

    An array that already holds its Arrow table yields that table's batches; otherwise its rows
    are converted (see :func:`rows_to_arrow_batches`), and the result is not kept on the array.
    """
    if array._backing.has_table:
        return iter(array._arrow_table().to_batches(max_chunksize=batch_size))
    return rows_to_arrow_batches(
        array.raw_rows, array.type, batch_size=batch_size, check_cancelled=check_cancelled
    )


def array_arrow_table(
    array: Array[object], check_cancelled: Callable[[], None] | None = None
) -> pa.Table:
    """``array``'s canonical Arrow table, converted from its rows the first time and then kept.

    How an array holds its rows is its own business, so the engines that bind one to DuckDB ask
    for the table here rather than reaching into it.
    """
    return array._arrow_table(check_cancelled)


def array_holds_rows(array: Array[object]) -> bool:
    """Whether ``array`` holds its Python rows, so a result may share them."""
    return array._backing.has_rows


def array_holds_arrow_table(array: Array[object]) -> bool:
    """Whether ``array`` already holds its table, so reading it converts nothing."""
    return array._backing.has_table


def rows_to_arrow_table(
    rows: Sequence[Row[FrozenValue]],
    array_type: ArrayType,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> pa.Table:
    """Canonical rows of ``array_type`` as its canonical Arrow table."""
    return pa.Table.from_batches(
        rows_to_arrow_batches(rows, array_type, check_cancelled=check_cancelled),
        schema=array_arrow_schema(array_type),
    )


def rows_to_arrow_batches(
    rows: Sequence[Row[FrozenValue]],
    array_type: ArrayType,
    *,
    batch_size: int = DEFAULT_ARROW_BATCH_SIZE,
    check_cancelled: Callable[[], None] | None = None,
) -> Iterator[pa.RecordBatch]:
    """Convert canonical rows of ``array_type`` into bounded canonical Arrow record batches.

    The conversion holds at most ``batch_size`` rows' column builders in Python at once.  It
    rejects non-finite floats, integers outside Arrow's signed 64-bit range, and malformed CIDs;
    those values cannot reach both Arrow and fxtr's canonical DAG-CBOR representation unchanged.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    schema = array_arrow_schema(array_type)
    columns = arrow_columns(array_type)
    dim_positions = {name: index for index, name in enumerate(array_type.dim_names)}
    transforms = [_needs_arrow_transform(column.schema) for column in columns]
    for offset in range(0, len(rows), batch_size):
        batch_rows = rows[offset : offset + batch_size]
        values: list[list[object]] = [[] for _ in columns]
        for key, value in cancellable(batch_rows, check_cancelled):
            for index, column in enumerate(columns):
                if column.role == "dimension":
                    dim_index = dim_positions[column.logical_name or ""]
                    values[index].append(key[dim_index])
                elif column.logical_name is None:
                    values[index].append(value)
                else:
                    assert isinstance(value, Mapping)
                    values[index].append(value[column.logical_name])
        arrays: list[pa.Array] = []
        for column_values, column, field, transform in zip(
            values, columns, schema, transforms, strict=True
        ):
            if check_cancelled is not None:
                check_cancelled()
            converted = column_values
            if transform:
                converted = []
                for value in cancellable(column_values, check_cancelled):
                    converted.append(_value_to_arrow(value, column.schema))
            try:
                arrow_values = pa.array(converted, type=field.type)
            except Exception as exc:
                # Preserve actionable fxtr validation errors, such as an out-of-range int,
                # without inspecting every ordinary value before Arrow's typed builder.
                for value in cancellable(column_values, check_cancelled):
                    _value_to_arrow(value, column.schema)
                if isinstance(exc, pa.ArrowException):
                    raise
                raise ArrowArrayError(f"Could not convert fxtr rows to Arrow: {exc}") from exc
            _require_finite_arrow_floats(arrow_values, column.schema)
            arrays.append(arrow_values)
        yield pa.RecordBatch.from_arrays(arrays, schema=schema)


def _needs_arrow_transform(schema: Schema) -> bool:
    match schema:
        case IdSchema():
            return True
        case NullableSchema(inner) | ListSchema(inner):
            return _needs_arrow_transform(inner)
        case StructSchema(fields):
            return not fields or any(_needs_arrow_transform(field) for field in fields.values())
        case _:
            return False


def _require_finite_arrow_floats(array: pa.Array, schema: Schema) -> None:
    match schema:
        case LeafSchema("float"):
            if bool(_pc_any(_pc_invert(_pc_is_finite(array))).as_py()):
                raise ArrowArrayError("An array cannot hold NaN or an infinite float")
        case NullableSchema(inner):
            _require_finite_arrow_floats(array, inner)
        case StructSchema(fields) if fields:
            assert isinstance(array, pa.StructArray)
            for name, field in fields.items():
                _require_finite_arrow_floats(array.field(name), field)
        case ListSchema(element):
            assert isinstance(array, pa.ListArray)
            _require_finite_arrow_floats(array.values, element)
        case _:
            pass


def _value_to_arrow(value: object, schema: Schema) -> object:
    # Values come from Python rows, which the schema has already checked, or from decoded
    # canonical data, which nothing has checked yet.
    match schema:
        case NullableSchema(inner):
            return None if value is None else _value_to_arrow(value, inner)
        case IdSchema():
            if not isinstance(value, EntityID):
                raise ArrowArrayError(f"Expected an entity ID, got {type(value).__name__}")
            _require_arrow_cid(value.cid)
            return value.cid
        case LeafSchema(kind):
            if type(value) is not LEAF_PYTHON_TYPES[kind]:
                raise ArrowArrayError(f"Expected {kind}, got {type(value).__name__}")
            if kind == "int" and not -(2**63) <= cast("int", value) < 2**63:
                raise ArrowArrayError(f"Integer {value} is outside Arrow int64 range")
            if kind == "float" and not math.isfinite(cast("float", value)):
                raise ArrowArrayError("An array cannot hold NaN or an infinite float")
            return value
        case ListSchema(element):
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ArrowArrayError(f"Expected a list, got {type(value).__name__}")
            return [_value_to_arrow(item, element) for item in value]
        case StructSchema(fields):
            if not isinstance(value, Mapping):
                raise ArrowArrayError(f"Expected a struct, got {type(value).__name__}")
            if set(value) != set(fields):
                raise ArrowArrayError(
                    f"Expected struct fields {sorted(fields)}, got {sorted(value)}"
                )
            if not fields:
                return False
            return {name: _value_to_arrow(value[name], fields[name]) for name in sorted(fields)}


def normalize_arrow_batch(batch: pa.RecordBatch, array_type: ArrayType) -> pa.RecordBatch:
    """Project and safely cast a DuckDB batch to the canonical schema for ``array_type``."""
    expected = array_arrow_schema(array_type)
    actual_names = batch.schema.names
    require_arrow_column_names(actual_names, array_type)
    arrays: list[pa.Array] = []
    for field in expected:
        column = batch.column(actual_names.index(field.name))
        if column.type != field.type:
            try:
                column = _normalize_arrow_array(column, field.type)
            except pa.ArrowException as exc:
                raise ArrowArrayError(
                    f"DuckDB column {field.name!r} cannot be losslessly cast from "
                    f"{column.type} to {field.type}"
                ) from exc
        if not field.nullable and column.null_count:
            raise ArrowArrayError(f"DuckDB column {field.name!r} contains nulls")
        arrays.append(column)
    return pa.RecordBatch.from_arrays(arrays, schema=expected)


def _normalize_arrow_array(array: pa.Array, expected: pa.DataType) -> pa.Array:
    if array.type == expected:
        return array
    # Nested field nullability describes visible values, not child slots hidden by a null
    # parent. Rebuild nested fields without casting those hidden slots to non-nullable data.
    if isinstance(array, pa.StructArray) and pa.types.is_struct(expected):
        fields = list(cast("pa.StructType", expected))
        if set(array.type.names) != {field.name for field in fields}:
            raise pa.ArrowInvalid("Nested struct fields do not match the declared value schema")
        return pa.StructArray.from_arrays(
            [_normalize_arrow_array(array.field(field.name), field.type) for field in fields],
            fields=fields,
            mask=array.is_null(),
        )
    if isinstance(array, pa.ListArray) and pa.types.is_list(expected):
        list_type = cast("pa.ListType", expected)
        return pa.Array.from_buffers(
            list_type,
            len(array),
            array.buffers()[:2],
            null_count=array.null_count,
            offset=array.offset,
            children=[_normalize_arrow_array(array.values, list_type.value_type)],
        )
    if not _castable_kind(array.type, expected):
        raise pa.ArrowInvalid(f"{array.type} is not a {expected} value")
    return pc.cast(array, expected, safe=True)


def _castable_kind(actual: pa.DataType, expected: pa.DataType) -> bool:
    """Whether a column DuckDB returned as ``actual`` holds values of ``expected``'s kind, so that
    casting it can only change its width or encoding.

    A safe Arrow cast refuses only what does not fit: it still turns ``7`` into ``True`` and a
    timestamp into its microseconds. A result of another kind is the query's mistake, not a
    representation to convert. An all-null column fits any type, and an integer is a float
    wherever it fits one exactly.
    """
    if pa.types.is_null(actual):
        return True
    if pa.types.is_dictionary(actual):
        return _castable_kind(cast("pa.DictionaryType", actual).value_type, expected)
    for same_kind in (
        pa.types.is_boolean,
        pa.types.is_integer,
        pa.types.is_floating,
        _is_text,
        _is_bytes,
    ):
        if same_kind(expected):
            return same_kind(actual) or (
                same_kind is pa.types.is_floating and pa.types.is_integer(actual)
            )
    return False


def _is_text(type: pa.DataType) -> bool:
    return (
        pa.types.is_string(type) or pa.types.is_large_string(type) or pa.types.is_string_view(type)
    )


def _is_bytes(type: pa.DataType) -> bool:
    return (
        pa.types.is_binary(type)
        or pa.types.is_large_binary(type)
        or pa.types.is_fixed_size_binary(type)
        or pa.types.is_binary_view(type)
    )


def require_arrow_column_names(actual_names: Sequence[str], array_type: ArrayType) -> None:
    """Require exactly one result column for every physical column of ``array_type``."""
    remaining = [column.physical_name for column in arrow_columns(array_type)]
    extras: list[str] = []
    for name in actual_names:
        try:
            remaining.remove(name)
        except ValueError:
            extras.append(name)
    missing = remaining
    if missing or extras:
        raise ArrowArrayError(
            f"DuckDB result columns differ from the declared array type; missing={missing}, "
            f"extra={extras}"
        )


def validate_arrow_batches(
    batches: Iterable[pa.RecordBatch], array_type: ArrayType
) -> tuple[list[pa.RecordBatch], int]:
    """Validate canonical schema, row cardinality, key order, and key uniqueness.

    The returned list contains the same batches. ``ArrowBatchValidator`` does the work one
    batch at a time, for callers that do not want to hold them all.
    """
    validator = ArrowBatchValidator(array_type)
    validated: list[pa.RecordBatch] = []
    for batch in batches:
        validator.accept(batch)
        validated.append(batch)
    validator.finish()
    return validated, validator.row_count


class ArrowBatchValidator:
    """Incrementally validate canonical Arrow batches without retaining their buffers."""

    def __init__(self, array_type: ArrayType) -> None:
        self.array_type = array_type
        self.row_count = 0
        self._previous_last: Key | None = None
        self._dim_columns = arrow_columns(array_type)[: len(array_type.dims)]

    def accept(self, batch: pa.RecordBatch) -> None:
        """Validate one next batch and update the running row/key state."""
        require_arrow_schema(batch.schema, self.array_type)
        for index, column in enumerate(arrow_columns(self.array_type)):
            if column.role == "value":
                _validate_arrow_values(batch.column(index), column.schema)
        _validate_batch_keys(batch, self._dim_columns)
        if batch.num_rows:
            first = self._key_at(batch, 0)
            last = self._key_at(batch, batch.num_rows - 1)
            if self._previous_last is not None and first <= self._previous_last:
                detail = "duplicate" if first == self._previous_last else "out-of-order"
                raise ArrowArrayError(f"Arrow batches have an {detail} key across batch boundaries")
            self._previous_last = last
        self.row_count += batch.num_rows

    def finish(self) -> None:
        """Validate whole-array cardinality after the final batch."""
        if not self.array_type.dims and self.row_count != 1:
            raise ArrowArrayError(
                f"An array with no dimensions must have exactly one row, got {self.row_count}"
            )

    def _key_at(self, batch: pa.RecordBatch, row_index: int) -> Key:
        return tuple(
            batch.column(batch.schema.get_field_index(column.physical_name))[row_index].as_py()
            for column in self._dim_columns
        )


def _validate_arrow_values(array: pa.Array, schema: Schema) -> None:
    # Arrow field metadata alone does not enforce child nullability or canonical value rules.
    # Nullable parents hide their child slots, so compact them before inspecting children.
    if isinstance(schema, NullableSchema):
        _validate_arrow_values(array.drop_null() if array.null_count else array, schema.inner)
        return
    if array.null_count:
        raise ArrowArrayError(f"Non-nullable schema {schema} contains a null")
    match schema:
        case LeafSchema("float"):
            _require_finite_arrow_floats(array, schema)
        case IdSchema():
            for cid in cast("list[bytes]", array.unique().to_pylist()):
                _require_arrow_cid(cid)
        case ListSchema(element):
            assert isinstance(array, pa.ListArray)
            _validate_arrow_values(array.flatten(), element)
        case StructSchema(fields):
            if not fields:
                if bool(_pc_any(array).as_py()):
                    raise ArrowArrayError("An Arrow empty-struct placeholder must be false")
                return
            assert isinstance(array, pa.StructArray)
            for name, field in fields.items():
                _validate_arrow_values(array.field(name), field)


def _validate_batch_keys(batch: pa.RecordBatch, dim_columns: Sequence[ArrowColumn]) -> None:
    if not dim_columns:
        return
    arrays = [
        batch.column(batch.schema.get_field_index(column.physical_name)) for column in dim_columns
    ]
    for column, array in zip(dim_columns, arrays, strict=True):
        if array.null_count:
            raise ArrowArrayError(f"Dimension {column.logical_name!r} contains null keys")
    if batch.num_rows < 2:
        return

    equal_prefix = _pc_equal(arrays[0].slice(1), arrays[0].slice(0, batch.num_rows - 1))
    out_of_order = _pc_less(arrays[0].slice(1), arrays[0].slice(0, batch.num_rows - 1))
    for array in arrays[1:]:
        current = array.slice(1)
        previous = array.slice(0, batch.num_rows - 1)
        out_of_order = _pc_or(out_of_order, _pc_and(equal_prefix, _pc_less(current, previous)))
        equal_prefix = _pc_and(equal_prefix, _pc_equal(current, previous))
    if bool(_pc_any(out_of_order).as_py()):
        raise ArrowArrayError("Arrow dimension keys are not sorted")
    if bool(_pc_any(equal_prefix).as_py()):
        raise ArrowArrayError("Arrow batches contain duplicate dimension keys")


def arrow_table_to_array(
    table: pa.Table,
    array_type: ArrayType,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> Array[object]:
    """Explicitly materialize a canonical Arrow table as a Python :class:`Array`."""
    require_arrow_schema(table.schema, array_type)
    validate_arrow_batches(table.to_batches(max_chunksize=DECODE_BATCH_SIZE), array_type)
    rows = arrow_rows(table, array_type, check_cancelled=check_cancelled)
    return Array._from_validated_rows(rows, array_type, canonical_order=True)


def array_from_validated_table(
    table: pa.Table,
    array_type: ArrayType,
    *,
    row_source: Array[object] | None = None,
) -> Array[object]:
    """An array holding ``table``, for a producer that has already checked it is canonical for
    ``array_type``. ``row_source`` is an input whose Python rows these are a subset of.
    """
    return Array._from_arrow(table, array_type, row_source=row_source)


def array_from_canonical_data(
    data: FxtrDataValue,
    array_type: ArrayType | None,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> Array[object]:
    """The array whose canonical entity data is ``data``, as a storage or the wire decodes it,
    holding an Arrow table built from ``data`` directly: no frozen rows are made.

    ``array_type`` is the type the data must have; ``None`` takes the type it declares. Every
    key and value is checked to be exactly what the type allows before Arrow sees it, because
    pyarrow would turn an int into a float or text into bytes, and ignore struct fields it was
    not asked for; the array would then have a different identity from the data. Rows must be in
    canonical order and unique.

    Raises:
        ArrowArrayError: If ``data`` is not an array of ``array_type`` in canonical form.
    """
    if (
        not isinstance(data, Mapping)
        or set(data) != {TYPE_KEY, "dims", "rows", "schema"}
        or data[TYPE_KEY] != ARRAY_TYPE
    ):
        raise ArrowArrayError("Array data must be a tagged mapping of 'dims', 'rows' and 'schema'")
    try:
        stored_type = ArrayType.from_wire({"dims": data["dims"], "schema": data["schema"]})
    except (ArrayError, KeyError, TypeError, ValueError) as exc:
        raise ArrowArrayError(f"Array data has a malformed type: {exc}") from exc
    if array_type is None:
        array_type = stored_type
    elif stored_type != array_type:
        raise ArrowArrayError(f"Array data has type {stored_type}, expected {array_type}")
    type_wire = array_type.to_wire()
    if data["dims"] != type_wire["dims"] or data["schema"] != type_wire["schema"]:
        raise ArrowArrayError("Array data's type is not in canonical form")
    rows = data["rows"]
    if not isinstance(rows, list):
        raise ArrowArrayError(f"Array rows must be a list, got {type(rows).__name__}")

    table = _checked_arrow_table(cast("list[object]", rows), array_type, check_cancelled)
    validate_arrow_batches(table.to_batches(max_chunksize=DEFAULT_ARROW_BATCH_SIZE), array_type)
    return Array._from_arrow(table, array_type)


def _checked_arrow_table(
    rows: list[object], array_type: ArrayType, check_cancelled: Callable[[], None] | None
) -> pa.Table:
    """The table of decoded rows, every key and value checked to be exactly what ``array_type``
    allows before pyarrow converts it (see :func:`array_from_canonical_data`)."""
    schema = array_arrow_schema(array_type)
    columns = arrow_columns(array_type)
    dim_count = len(array_type.dims)
    key_types = [LEAF_PYTHON_TYPES[dim.kind] for dim in array_type.dims]
    struct_fields = (
        set(cast("StructSchema", array_type.schema).fields)
        if _one_column_per_field(array_type.schema)
        else None
    )
    batches: list[pa.RecordBatch] = []
    for offset in range(0, len(rows), DEFAULT_ARROW_BATCH_SIZE):
        values: list[list[object]] = [[] for _ in columns]
        batch_rows = rows[offset : offset + DEFAULT_ARROW_BATCH_SIZE]
        for index, row in enumerate(cancellable(batch_rows, check_cancelled), start=offset):
            if not isinstance(row, list) or len(cast("list[object]", row)) != dim_count + 1:
                raise ArrowArrayError(f"row {index}: expected {dim_count} keys and a value")
            cells = cast("list[object]", row)
            for position, key_type in enumerate(key_types):
                if type(cells[position]) is not key_type:
                    name = array_type.dims[position].name
                    raise ArrowArrayError(f"row {index}: dimension {name!r} has the wrong kind")
                values[position].append(cells[position])
            value = cells[-1]
            try:
                if struct_fields is None:
                    values[-1].append(_value_to_arrow(value, array_type.schema))
                    continue
                if not isinstance(value, Mapping):
                    raise ArrowArrayError(f"Expected a struct, got {type(value).__name__}")
                fields = cast("Mapping[str, object]", value)
                if set(fields) != struct_fields:
                    raise ArrowArrayError(
                        f"Expected struct fields {sorted(struct_fields)}, got {sorted(fields)}"
                    )
                for position, column in enumerate(columns[dim_count:], start=dim_count):
                    assert column.logical_name is not None
                    values[position].append(
                        _value_to_arrow(fields[column.logical_name], column.schema)
                    )
            except ArrowArrayError as exc:
                raise ArrowArrayError(f"row {index}: {exc}") from exc
        arrays = [
            pa.array(column_values, type=field.type)
            for column_values, field in zip(values, schema, strict=True)
        ]
        batches.append(pa.RecordBatch.from_arrays(arrays, schema=schema))
    return pa.Table.from_batches(batches, schema=schema)


DECODE_BATCH_SIZE = 4096
"""Rows decoded into Python at a time, bounding the temporary lists and dicts even when a table
is one very large chunk."""


def arrow_rows(
    table: pa.Table,
    array_type: ArrayType,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> Iterator[Row[FrozenValue]]:
    """The canonical rows of a validated canonical table of ``array_type``, in its order.

    Decodes :data:`DECODE_BATCH_SIZE` rows at a time, so iterating holds one batch of Python
    values, not every row.
    """
    columns = arrow_columns(array_type)
    dim_count = len(array_type.dims)
    value_columns = columns[dim_count:]
    struct_value = isinstance(array_type.schema, StructSchema) and bool(array_type.schema.fields)
    for batch in table.to_batches(max_chunksize=DECODE_BATCH_SIZE):
        if check_cancelled is not None:
            check_cancelled()
        dim_values = [
            cast("list[int | str]", batch.column(i).to_pylist()) for i in range(dim_count)
        ]
        value_values = [
            _arrow_array_to_values(batch.column(i), column.schema, check_cancelled)
            for i, column in enumerate(value_columns, start=dim_count)
        ]
        for row_index in cancellable(range(batch.num_rows), check_cancelled):
            key: Key = tuple(values[row_index] for values in dim_values)
            if struct_value:
                value: FrozenValue = frozendict(
                    {
                        column.logical_name: values[row_index]
                        for column, values in zip(value_columns, value_values, strict=True)
                        if column.logical_name is not None
                    }
                )
            else:
                value = value_values[0][row_index]
            yield key, value


def arrow_keys(table: pa.Table, array_type: ArrayType) -> Iterator[Key]:
    """The dimension keys of a canonical table of ``array_type``, in its order; no values are
    decoded."""
    dim_count = len(array_type.dims)
    for batch in table.to_batches(max_chunksize=DECODE_BATCH_SIZE):
        if not dim_count:
            yield from (() for _ in range(batch.num_rows))
            continue
        columns = [cast("list[int | str]", batch.column(i).to_pylist()) for i in range(dim_count)]
        yield from zip(*columns, strict=True)


# ---- Array operations over Arrow ----
# Each function below computes the canonical table of an Array operation's result from its
# inputs' canonical tables. Array uses them when an input holds no Python rows, so that
# operating on a query result does not decode it. Unchanged values keep their Arrow buffers.


def _one_column_per_field(schema: Schema) -> bool:
    """Whether a value of ``schema`` is laid out as one Arrow column per struct field (see
    :func:`arrow_columns`), rather than as one ``value`` column."""
    return isinstance(schema, StructSchema) and bool(schema.fields)


def _whole_values(table: pa.Table, array_type: ArrayType) -> pa.ChunkedArray:
    """The values of a canonical table of ``array_type`` as one column, whatever its layout."""
    dim_count = len(array_type.dims)
    if not _one_column_per_field(array_type.schema):
        return table.column(dim_count)
    struct_type = _arrow_data_type(array_type.schema)
    # Batches, not columns: the columns of a table may be chunked differently, and a batch
    # slices all of them at the same rows.
    chunks = [
        pa.StructArray.from_arrays(batch.columns[dim_count:], fields=list(struct_type))
        for batch in table.to_batches()
    ]
    return pa.chunked_array(chunks, type=struct_type)


def _table_of(
    dims: Sequence[pa.ChunkedArray | pa.Array], values: pa.ChunkedArray, array_type: ArrayType
) -> pa.Table:
    """The canonical table of ``array_type`` from its dim columns, in order, and its values as
    one column of its value schema's Arrow type."""
    value_columns = values.flatten() if _one_column_per_field(array_type.schema) else [values]
    return pa.Table.from_arrays([*dims, *value_columns], schema=array_arrow_schema(array_type))


def _sorted_by_keys(table: pa.Table, array_type: ArrayType) -> pa.Table:
    """``table`` with its rows in canonical key order. Arrow orders strings by their UTF-8
    bytes, which is code point order, as Python orders them."""
    dims = arrow_columns(array_type)[: len(array_type.dims)]
    return table.sort_by([(column.physical_name, "ascending") for column in dims])


def retype_arrow_table(table: pa.Table, source: ArrayType, target: ArrayType) -> pa.Table:
    """A canonical table of ``source`` as one of ``target``, which ``source`` is assignable to:
    the dims in ``target``'s order and the rows sorted to match, and the value schema's Arrow
    types, nullability and metadata ``target``'s."""
    positions = [source.dim_names.index(name) for name in target.dim_names]
    values = _whole_values(table, source).cast(_arrow_data_type(target.schema))
    retyped = _table_of([table.column(position) for position in positions], values, target)
    if positions != sorted(positions):
        retyped = _sorted_by_keys(retyped, target)
    return retyped


def stack_arrow_tables(
    parts: Sequence[tuple[Key, pa.Table, ArrayType]], target: ArrayType
) -> pa.Table:
    """The canonical table of :meth:`Array.stack_over`: each part's table under its key along
    ``target``'s leading dims, its own dims in ``target``'s order after them.

    Each part is put in ``target``'s dim order first (parts match dims by name, so they may hold
    them in another order); they then have one type, so their tables concatenate into one without
    copying, and each column is built once for the whole result rather than once per part: a
    mapped step's results are thousands of one-row parts.
    """
    if not parts:
        return array_arrow_schema(target).empty_table()
    stacked_count = len(target.dims) - len(parts[0][2].dims)
    part_type = ArrayType(target.dims[stacked_count:], target.schema)
    combined = pa.concat_tables(
        [
            table
            if array_type.dim_names == part_type.dim_names
            else retype_arrow_table(table, array_type, part_type)
            for _, table, array_type in parts
        ]
    )
    # Which part each row of the result came from, so each key column is one take.
    owner = pa.array(
        list(
            itertools.chain.from_iterable(
                itertools.repeat(index, table.num_rows) for index, (_, table, _) in enumerate(parts)
            )
        ),
        pa.int32(),
    )
    leading = [
        pa.array(
            [key[position] for key, _, _ in parts], type=_arrow_data_type(LeafSchema(dim.kind))
        ).take(owner)
        for position, dim in enumerate(target.dims[:stacked_count])
    ]
    inner = [
        combined.column(part_type.dim_names.index(name))
        for name in target.dim_names[stacked_count:]
    ]
    values = _whole_values(combined, part_type)
    return _sorted_by_keys(_table_of([*leading, *inner], values, target), target)


def combine_arrow_tables(
    inputs: Mapping[str, tuple[pa.Table, ArrayType, Sequence[int | None]]],
    keys: Sequence[Key],
    target: ArrayType,
) -> pa.Table:
    """The canonical table of :meth:`Array.combine`: for each of ``keys``, a struct holding each
    input's value at the given row position, or null where it has none."""
    dims = [
        pa.array([key[index] for key in keys], type=_arrow_data_type(LeafSchema(dim.kind)))
        for index, dim in enumerate(target.dims)
    ]
    columns: dict[str, pa.ChunkedArray] = {}
    for name, (table, array_type, positions) in inputs.items():
        columns[name] = _whole_values(table, array_type).take(pa.array(positions, pa.int64()))
    schema = cast("StructSchema", target.schema)
    return pa.Table.from_arrays(
        [*dims, *(columns[name] for name in schema.names)], schema=array_arrow_schema(target)
    )


def _arrow_array_to_values(
    array: pa.Array,
    schema: Schema,
    check_cancelled: Callable[[], None] | None,
) -> list[FrozenValue]:
    if check_cancelled is not None:
        check_cancelled()
    if isinstance(schema, NullableSchema):
        if isinstance(schema.inner, LeafSchema):
            return cast("list[FrozenValue]", array.to_pylist())
        if not array.null_count:
            return _arrow_array_to_values(array, schema.inner, check_cancelled)
        # Compact valid parents before decoding children: hidden child nulls and IDs must not
        # be validated. Scattering preserves the original nullable row positions.
        present = iter(_arrow_array_to_values(array.drop_null(), schema.inner, check_cancelled))
        validity = cast("list[bool]", array.is_valid().to_pylist())
        nullable: list[FrozenValue] = []
        for valid in cancellable(validity, check_cancelled):
            nullable.append(next(present) if valid else None)
        return nullable
    if array.null_count:
        raise ArrowArrayError(f"Non-nullable schema {schema} contains a null")
    if isinstance(schema, LeafSchema):
        return cast("list[FrozenValue]", array.to_pylist())
    if isinstance(schema, ListSchema):
        assert isinstance(array, pa.ListArray)
        offsets = cast("list[int]", array.offsets.to_pylist())
        first, last = offsets[0], offsets[-1]
        elements = _arrow_array_to_values(
            array.values.slice(first, last - first),
            schema.element,
            check_cancelled,
        )
        lists: list[FrozenValue] = []
        for start, end in cancellable(zip(offsets[:-1], offsets[1:], strict=True), check_cancelled):
            lists.append(tuple(elements[start - first : end - first]))
        return lists
    if isinstance(schema, StructSchema) and schema.fields:
        assert isinstance(array, pa.StructArray)
        names = sorted(schema.fields)
        fields = [
            _arrow_array_to_values(array.field(name), schema.fields[name], check_cancelled)
            for name in names
        ]
        values: list[FrozenValue] = []
        for row in cancellable(zip(*fields, strict=True), check_cancelled):
            values.append(frozendict(zip(names, row, strict=True)))
        return values

    decoded: list[FrozenValue] = []
    for value in cancellable(array.to_pylist(), check_cancelled):
        if isinstance(schema, IdSchema):
            assert isinstance(value, bytes)
            _require_arrow_cid(value)
            decoded.append(EntityID(cid=value))
        else:
            assert isinstance(schema, StructSchema) and not schema.fields
            if value is not False:
                raise ArrowArrayError("Arrow empty-struct placeholder must be false")
            decoded.append(frozendict())
    return decoded


def arrow_table_content_id(
    table: pa.Table,
    array_type: ArrayType,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> EntityID:
    """The content ID of the array's canonical bytes, written from the table, computed without
    keeping them."""
    writer = _CborWriter()
    _write_arrow_table(writer, table, array_type, check_cancelled)
    return writer.entity_id


def _write_arrow_table(
    writer: _CborWriter,
    table: pa.Table,
    array_type: ArrayType,
    check_cancelled: Callable[[], None] | None,
) -> None:
    """Write ``table``'s canonical bytes."""
    require_arrow_schema(table.schema, array_type)
    # Bounded batches bound the Python lists each one is turned into below.
    batches, row_count = validate_arrow_batches(
        table.to_batches(max_chunksize=DEFAULT_ARROW_BATCH_SIZE), array_type
    )
    # Validated batches hold the columns of ``arrow_columns`` in order: dimensions, then either
    # one column per field of a struct value or the single value column.
    columns = arrow_columns(array_type)
    dimension_count = len(array_type.dims)
    write_dimensions = [_value_writer(column.schema) for column in columns[:dimension_count]]
    struct_value = isinstance(array_type.schema, StructSchema) and bool(array_type.schema.fields)
    value_columns = {
        column.logical_name: index for index, column in enumerate(columns) if column.role == "value"
    }
    write_fields = (
        [
            (name, value_columns[name], _value_writer(field))
            for name, field in codec.canonical_map_items(
                cast("StructSchema", array_type.schema).fields
            )
        ]
        if struct_value
        else []
    )
    write_whole_value = _value_writer(columns[-1].schema)
    type_wire = array_type.to_wire()

    writer.map_header(4)
    writer.text("dims")
    writer.raw(codec.encode(type_wire["dims"]))
    writer.text("rows")
    writer.array_header(row_count)
    for batch in batches:
        # One conversion per column is far cheaper than boxing an Arrow scalar per cell.
        values = [column.to_pylist() for column in batch.columns]
        for row_index in cancellable(range(batch.num_rows), check_cancelled):
            writer.array_header(dimension_count + 1)
            for index, write in enumerate(write_dimensions):
                write(writer, values[index][row_index])
            if struct_value:
                writer.map_header(len(write_fields))
                for name, index, write in write_fields:
                    writer.text(name)
                    write(writer, values[index][row_index])
            else:
                write_whole_value(writer, values[-1][row_index])
    writer.text("$type")
    writer.text(ARRAY_TYPE)
    writer.text("schema")
    writer.raw(codec.encode(type_wire["schema"]))


type _ValueWriter = Callable[["_CborWriter", object], None]
"""Writes one Python value, as pyarrow's ``to_pylist`` produces it."""


def _value_writer(schema: Schema) -> _ValueWriter:
    """The writer for values of ``schema``.

    A writer is built once per column, so everything that depends only on the schema, such as a
    struct's canonical field order, is worked out here rather than again for every row.
    """
    if isinstance(schema, NullableSchema):
        write_present = _value_writer(schema.inner)

        def write_nullable(writer: _CborWriter, value: object) -> None:
            if value is None:
                writer.null()
            else:
                write_present(writer, value)

        return write_nullable

    write = _present_value_writer(schema)

    def write_required(writer: _CborWriter, value: object) -> None:
        if value is None:
            raise ArrowArrayError(f"Non-nullable schema {schema} contains a null")
        write(writer, value)

    return write_required


def _present_value_writer(schema: Schema) -> _ValueWriter:
    match schema:
        case IdSchema():

            def write_id(writer: _CborWriter, value: object) -> None:
                if not isinstance(value, bytes) or len(value) != 36:
                    raise ArrowArrayError("Arrow entity IDs must be 36-byte CID values")
                writer.link(value)

            return write_id
        case LeafSchema("int"):

            def write_int(writer: _CborWriter, value: object) -> None:
                assert type(value) is int
                writer.integer(value)

            return write_int
        case LeafSchema("float"):

            def write_float(writer: _CborWriter, value: object) -> None:
                assert type(value) is float
                writer.float(value)

            return write_float
        case LeafSchema("bool"):

            def write_bool(writer: _CborWriter, value: object) -> None:
                assert type(value) is bool
                writer.boolean(value)

            return write_bool
        case LeafSchema("str"):

            def write_str(writer: _CborWriter, value: object) -> None:
                assert type(value) is str
                writer.text(value)

            return write_str
        case LeafSchema("bytes"):

            def write_bytes(writer: _CborWriter, value: object) -> None:
                assert type(value) is bytes
                writer.bytes(value)

            return write_bytes
        case ListSchema(element):
            write_element = _value_writer(element)

            def write_list(writer: _CborWriter, value: object) -> None:
                assert isinstance(value, list)
                items = cast("list[object]", value)
                writer.array_header(len(items))
                for item in items:
                    write_element(writer, item)

            return write_list
        case StructSchema(fields) if not fields:

            def write_empty(writer: _CborWriter, value: object) -> None:
                if value is not False:
                    raise ArrowArrayError("An empty struct must be represented by a false boolean")
                writer.map_header(0)

            return write_empty
        case StructSchema(fields):
            ordered = [
                (name, _value_writer(field)) for name, field in codec.canonical_map_items(fields)
            ]

            def write_struct(writer: _CborWriter, value: object) -> None:
                assert isinstance(value, dict)
                record = cast("dict[str, object]", value)
                writer.map_header(len(ordered))
                for name, write_field in ordered:
                    writer.text(name)
                    write_field(writer, record[name])

            return write_struct
        case _:
            raise ArrowArrayError(f"Cannot encode values of schema {schema}")


class _CborWriter:
    """The strict DAG-CBOR subset used by an array's known schema, written only to be hashed:
    it computes the content ID of the bytes without keeping them."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()

    @property
    def entity_id(self) -> EntityID:
        """The content id of the bytes written so far."""
        return EntityID(cid=b"\x01\x71\x12\x20" + self._hash.digest())

    def raw(self, data: bytes) -> None:
        self._hash.update(data)

    def _major(self, major: int, value: int) -> None:
        if value < 0:
            raise ArrowArrayError(f"CBOR length/value must be non-negative, got {value}")
        prefix = major << 5
        if value < 24:
            self.raw(bytes([prefix | value]))
        elif value < 2**8:
            self.raw(bytes([prefix | 24, value]))
        elif value < 2**16:
            self.raw(bytes([prefix | 25]) + value.to_bytes(2, "big"))
        elif value < 2**32:
            self.raw(bytes([prefix | 26]) + value.to_bytes(4, "big"))
        elif value < 2**64:
            self.raw(bytes([prefix | 27]) + value.to_bytes(8, "big"))
        else:
            raise ArrowArrayError(f"Integer {value} is outside the DAG-CBOR range")

    def integer(self, value: int) -> None:
        self._major(0, value) if value >= 0 else self._major(1, -1 - value)

    def bytes(self, value: bytes) -> None:
        self._major(2, len(value))
        self.raw(value)

    def text(self, value: str) -> None:
        encoded = value.encode()
        self._major(3, len(encoded))
        self.raw(encoded)

    def array_header(self, length: int) -> None:
        self._major(4, length)

    def map_header(self, length: int) -> None:
        self._major(5, length)

    def boolean(self, value: bool) -> None:
        self.raw(b"\xf5" if value else b"\xf4")

    def null(self) -> None:
        self.raw(b"\xf6")

    def float(self, value: float) -> None:
        if not math.isfinite(value):
            raise ArrowArrayError("DAG-CBOR cannot encode NaN or infinite floats")
        self.raw(b"\xfb" + struct.pack(">d", value))

    def link(self, cid: bytes) -> None:
        _require_arrow_cid(cid)
        self.raw(b"\xd8\x2a")
        self.bytes(b"\x00" + cid)
