"""JSON encodings for array values, keys, and array types, directed by their schemas.

Row values of project arrays are stored as ``jsonb`` so they can be read from SQL. JSON cannot
express entity ids, bytes, or the int/float distinction on its own, so nothing here is
self-describing: the *schema* is the source of truth and sits outside the value. An ``id``
position holds a cid string, a ``bytes`` position holds base64, a ``float`` position may hold
what JSON prints as an integer, and everything else is plain JSON; decoding walks the same
schema. No key is reserved, so a struct field may be called anything.

The declared array type is stored the same way, using the fixed grammar of its wire form (see
:mod:`fxtr.entity_defns.schema`): a typed id's type id appears as a string under the ``"id"``
key.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from frozendict import frozendict

from fxtr.core.entity_data import EntityID, cid_from_str, cid_to_str
from fxtr.entity_defns.array import ArrayType, Dim, Dims, Key, KeyValue, as_dims
from fxtr.entity_defns.schema import (
    LEAF_KINDS,
    IdSchema,
    LeafSchema,
    ListSchema,
    NullableSchema,
    Schema,
    StructSchema,
)

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
"""What the ``json`` module and ``jsonb`` columns exchange."""


class JsonValueError(ValueError):
    """JSON data does not fit the schema it is being decoded with."""


# ---- values ----


def value_to_json(value: object, schema: Schema, where: str = "value") -> Json:
    """Encode a value (in frozen or plain form) that fits ``schema``.

    Args:
        value: The value; assumed to already satisfy the schema (see
            :func:`~fxtr.entity_defns.schema.coerce_value`).
        schema: The schema directing the encoding.
        where: Names the value in error messages.

    Raises:
        JsonValueError: If the value's shape does not match the schema.
    """
    match schema:
        case NullableSchema(inner):
            return None if value is None else value_to_json(value, inner, where)
        case LeafSchema("bytes"):
            if not isinstance(value, bytes):
                raise JsonValueError(f"{where}: expected bytes, got {type(value).__name__}")
            return base64.b64encode(value).decode("ascii")
        case LeafSchema("float"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise JsonValueError(f"{where}: expected a float, got {type(value).__name__}")
            return float(value)
        case LeafSchema(kind):
            if type(value) is not _LEAF_TYPES[kind]:
                raise JsonValueError(f"{where}: expected {kind}, got {type(value).__name__}")
            return value  # type: ignore[return-value]
        case IdSchema():
            if not isinstance(value, EntityID):
                raise JsonValueError(f"{where}: expected an entity id, got {type(value).__name__}")
            return cid_to_str(value.cid)
        case ListSchema(element):
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise JsonValueError(f"{where}: expected a list, got {type(value).__name__}")
            return [value_to_json(v, element, f"{where}[{i}]") for i, v in enumerate(value)]
        case StructSchema(fields):
            if not isinstance(value, Mapping) or set(value) != set(fields):
                raise JsonValueError(f"{where}: expected a struct with fields {sorted(fields)}")
            return {
                name: value_to_json(value[name], s, f"{where}.{name}") for name, s in fields.items()
            }
    raise JsonValueError(f"{where}: unknown schema {schema!r}")


def value_from_json(data: object, schema: Schema, where: str = "value") -> Any:
    """Decode JSON data under ``schema`` into the plain value shape
    (:data:`~fxtr.entity_defns.schema.ValueInput`; lists and dicts, ids as bare
    :class:`~fxtr.core.entity_data.EntityID`).

    Args:
        data: The JSON data, as the ``json`` module returns it.
        schema: The schema directing the decoding.
        where: Names the value in error messages.

    Raises:
        JsonValueError: If the data does not fit the schema.
    """
    match schema:
        case NullableSchema(inner):
            return None if data is None else value_from_json(data, inner, where)
        case LeafSchema("bytes"):
            if not isinstance(data, str):
                raise JsonValueError(
                    f"{where}: expected a base64 string, got {type(data).__name__}"
                )
            try:
                return base64.b64decode(data, validate=True)
            except ValueError as exc:
                raise JsonValueError(f"{where}: invalid base64") from exc
        case LeafSchema("float"):
            if isinstance(data, bool) or not isinstance(data, (int, float)):
                raise JsonValueError(f"{where}: expected a number, got {type(data).__name__}")
            return float(data)
        case LeafSchema(kind):
            if type(data) is not _LEAF_TYPES[kind]:
                raise JsonValueError(f"{where}: expected {kind}, got {type(data).__name__}")
            return data
        case IdSchema():
            if not isinstance(data, str):
                raise JsonValueError(f"{where}: expected a cid string, got {type(data).__name__}")
            try:
                return EntityID(cid=cid_from_str(data))
            except ValueError as exc:
                raise JsonValueError(f"{where}: {exc}") from exc
        case ListSchema(element):
            if not isinstance(data, list):
                raise JsonValueError(f"{where}: expected a list, got {type(data).__name__}")
            return [value_from_json(v, element, f"{where}[{i}]") for i, v in enumerate(data)]
        case StructSchema(fields):
            if not isinstance(data, dict) or set(data) != set(fields):
                raise JsonValueError(f"{where}: expected an object with fields {sorted(fields)}")
            return {
                name: value_from_json(data[name], s, f"{where}.{name}")
                for name, s in fields.items()
            }
    raise JsonValueError(f"{where}: unknown schema {schema!r}")


_LEAF_TYPES: dict[str, type] = {
    "int": int,
    "float": float,
    "bool": bool,
    "str": str,
    "bytes": bytes,
}


# ---- keys ----


def key_to_json(key: Key) -> list[Json]:
    """Encode a key (values in dim order); ints and strings are JSON as they are."""
    return list(key)


def key_from_json(data: object, dims: Dims, where: str = "key") -> Key:
    """Decode a key under its dims.

    Raises:
        JsonValueError: If the data is not a list of the right length holding each dim's kind.
    """
    if not isinstance(data, list) or len(data) != len(dims):
        raise JsonValueError(f"{where}: expected {len(dims)} key values, got {data!r}")
    key: list[KeyValue] = []
    for dim, value in zip(dims, data, strict=True):
        if type(value) is not _LEAF_TYPES[dim.kind]:
            raise JsonValueError(f"{where}: dim {dim.name!r} expects {dim.kind}, got {value!r}")
        key.append(value)
    return tuple(key)


# ---- array types ----


def schema_to_json(schema: Schema) -> Json:
    """Encode a schema: its wire form (see :func:`~fxtr.entity_defns.schema.schema_to_wire`),
    which is already JSON-shaped."""
    match schema:
        case LeafSchema(kind):
            return kind
        case IdSchema(None):
            return "id"
        case IdSchema(type_id):
            assert type_id is not None
            return {"id": type_id}
        case ListSchema(element):
            return {"list": schema_to_json(element)}
        case StructSchema(fields):
            return {"struct": {name: schema_to_json(s) for name, s in fields.items()}}
        case NullableSchema(inner):
            return {"nullable": schema_to_json(inner)}
    raise JsonValueError(f"Unknown schema {schema!r}")


def schema_from_json(data: object, where: str = "schema") -> Schema:
    """Decode a schema (strictly) from :func:`schema_to_json`'s form.

    Raises:
        JsonValueError: If the data is not a valid schema.
    """
    if isinstance(data, str):
        if data == "id":
            return IdSchema()
        if data not in LEAF_KINDS:
            raise JsonValueError(f"{where}: unknown leaf schema {data!r}")
        return LeafSchema(data)  # type: ignore[arg-type]
    if isinstance(data, dict) and len(data) == 1:
        ((tag, inner),) = data.items()
        if tag == "id":
            if not isinstance(inner, str) or not inner:
                raise JsonValueError(
                    f"{where}: an id's type must be a non-empty string, got {inner!r}"
                )
            return IdSchema(inner)
        if tag == "list":
            return ListSchema(schema_from_json(inner, f"{where}.list"))
        if tag == "nullable":
            return NullableSchema(schema_from_json(inner, f"{where}.nullable"))
        if tag == "struct":
            if not isinstance(inner, dict):
                raise JsonValueError(f"{where}: struct fields must be an object, got {inner!r}")
            return StructSchema(
                frozendict(
                    {name: schema_from_json(s, f"{where}.{name}") for name, s in inner.items()}
                )
            )
    raise JsonValueError(f"{where}: invalid schema data {data!r}")


def array_type_to_json(ttype: ArrayType) -> dict[str, Json]:
    """Encode an array type as ``{"dims": [[name, kind], ...], "schema": ...}``."""
    return {
        "dims": [[dim.name, dim.kind] for dim in ttype.dims],
        "schema": schema_to_json(ttype.schema),
    }


def array_type_from_json(data: object) -> ArrayType:
    """Decode an array type from :func:`array_type_to_json`'s form.

    Raises:
        JsonValueError: If the data is not a valid array type.
    """
    if not isinstance(data, dict) or set(data) != {"dims", "schema"}:
        raise JsonValueError(f"Array type data must have exactly 'dims' and 'schema', got {data!r}")
    raw_dims = data["dims"]
    if not isinstance(raw_dims, list):
        raise JsonValueError(f"Dims must be a list, got {raw_dims!r}")
    dims: list[Dim] = []
    for item in raw_dims:
        if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)):
            raise JsonValueError(f"Dim must be a [name, kind] pair, got {item!r}")
        dims.append(Dim(item[0], item[1]))
    return ArrayType(dims=as_dims(dims), schema=schema_from_json(data["schema"]))
