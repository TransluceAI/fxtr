"""The schema-directed JSON encoding of array values, keys, and array types."""

from __future__ import annotations

import json

import pytest
from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import ArrayType
from fxtr.entity_defns.schema import (
    IdSchema,
    LeafSchema,
    StructSchema,
    as_schema,
    coerce_value,
    list_of,
    optional,
    struct_of,
)

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres.json_values import (  # noqa: E402
    JsonValueError,
    array_type_from_json,
    array_type_to_json,
    key_from_json,
    key_to_json,
    value_from_json,
    value_to_json,
)

NODE_TYPE = "test.fxtr.node"
SOME_ID = EntityID(cid=codec.compute_cid(codec.encode({"$type": "fxtr.builtin.testing.node"})))


def through_json(data: object) -> object:
    """What survives a trip through the ``json`` module (as jsonb does)."""
    return json.loads(json.dumps(data))


@pytest.mark.parametrize(
    ("spec", "value"),
    [
        (int, 3),
        (float, 2.5),
        (float, 2.0),
        (bool, True),
        (str, "hi"),
        (bytes, b"\x00\xff"),
        (IdSchema(), SOME_ID),
        (IdSchema(NODE_TYPE), SOME_ID),
        (optional(int), None),
        (optional(int), 4),
        (list_of(str), ["a", "b"]),
        (list_of(IdSchema()), [SOME_ID, SOME_ID]),
        (
            struct_of(a=int, b=optional(bytes), c=list_of(struct_of(d=float))),
            {"a": 1, "b": None, "c": [{"d": 1.0}]},
        ),
    ],
)
def test_value_round_trip(spec: object, value: object) -> None:
    schema = as_schema(spec)
    frozen = coerce_value(value, schema)
    data = through_json(value_to_json(frozen, schema))
    assert coerce_value(value_from_json(data, schema), schema) == frozen


def test_no_field_name_is_reserved() -> None:
    schema = StructSchema(frozendict({"/": LeafSchema("str"), "id": LeafSchema("int")}))
    value = coerce_value({"/": "slash", "id": 7}, schema)
    data = through_json(value_to_json(value, schema))
    assert data == {"/": "slash", "id": 7}
    assert coerce_value(value_from_json(data, schema), schema) == value


def test_float_positions_accept_integral_json_numbers() -> None:
    schema = as_schema(float)
    assert value_to_json(2.0, schema) == 2.0
    assert value_from_json(2, schema) == 2.0
    assert isinstance(value_from_json(2, schema), float)


def test_value_decoding_is_strict() -> None:
    with pytest.raises(JsonValueError):
        value_from_json(1.5, as_schema(int))
    with pytest.raises(JsonValueError):
        value_from_json(True, as_schema(int))
    with pytest.raises(JsonValueError):
        value_from_json("not base64!", as_schema(bytes))
    with pytest.raises(JsonValueError):
        value_from_json("not a cid", as_schema(IdSchema()))
    with pytest.raises(JsonValueError):
        value_from_json({"a": 1}, as_schema(struct_of(a=int, b=int)))
    with pytest.raises(JsonValueError):
        value_from_json(None, as_schema(str))
    with pytest.raises(JsonValueError):
        value_to_json("x", as_schema(int))


def test_key_round_trip() -> None:
    ttype = ArrayType.of(("[prompt: int, split: str]", int))
    key = (3, "a")
    assert key_from_json(through_json(key_to_json(key)), ttype.dims) == key
    with pytest.raises(JsonValueError):
        key_from_json([3], ttype.dims)
    with pytest.raises(JsonValueError):
        key_from_json(["3", "a"], ttype.dims)


@pytest.mark.parametrize(
    "spec",
    [
        str,
        ("[]", IdSchema()),
        ("[prompt: int]", str),
        (
            "[p: int, split: str]",
            struct_of(bio=IdSchema(NODE_TYPE), n=optional(float), t=list_of(str)),
        ),
        ("[x: str]", struct_of(id=str, list=int, struct=bytes)),
    ],
)
def test_array_type_round_trip(spec: object) -> None:
    ttype = ArrayType.of(spec)
    data = through_json(array_type_to_json(ttype))
    assert array_type_from_json(data) == ttype


def test_array_type_decoding_is_strict() -> None:
    with pytest.raises(JsonValueError):
        array_type_from_json({"dims": [], "schema": "nope"})
    with pytest.raises(JsonValueError):
        array_type_from_json({"dims": [["prompt"]], "schema": "int"})
    with pytest.raises(JsonValueError):
        array_type_from_json({"dims": [], "schema": {"id": 3}})
    with pytest.raises(JsonValueError):
        array_type_from_json({"schema": "int"})
