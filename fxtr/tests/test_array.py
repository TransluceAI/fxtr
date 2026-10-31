"""Tests for arrays: dims and array types, construction and access, the operations, and
serialization round trips."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any, Literal, TypedDict

import pytest
from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import (
    ARRAY_TYPE,
    Array,
    ArrayError,
    ArrayType,
    ArrayTypeError,
    Dim,
    PartialArrayType,
    as_dims,
    combine_type,
    field_type,
    is_assignable_array,
    join_keys,
    parse_dims,
    rename_type,
    sel_type,
    stack_over_type,
    stack_type,
    union_dims,
)
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import ConversionError
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.schema import (
    FrozenValue,
    IdSchema,
    LeafSchema,
    NullableSchema,
    id_of,
    list_of,
    optional,
    struct_of,
)
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio


LEAF_TYPE = "test.fxtr.array.leaf"


@dataclasses.dataclass(frozen=True, kw_only=True)
class Leaf(DataclassEntity, fxtr_type=LEAF_TYPE):
    label: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Holder(DataclassEntity, fxtr_type="test.fxtr.array.holder"):
    array: BoundID[Array[str]]


DIMS = "id: int, split: str"
SCHEMA = struct_of(score=optional(float), tags=list_of(str))
TYPE = (DIMS, SCHEMA)
RECORDS = [
    {"id": 2, "split": "a", "score": None, "tags": ["x", "y"]},
    {"id": 1, "split": "b", "score": 0.5, "tags": []},
    {"id": 1, "split": "a", "score": 1.5, "tags": ["z"]},
]


def make_array() -> Array[frozendict]:
    return Array.from_records(RECORDS, TYPE)


def scores(*items: tuple[int, float]) -> Array[float]:
    return Array.from_items([((k,), v) for k, v in items], ("[id: int]", float))


# ---- dims and array types ----


def test_dims_parsing_and_normalization() -> None:
    dims = parse_dims("prompt: int, sample: str")
    assert dims == (Dim("prompt", "int"), Dim("sample", "str"))
    assert parse_dims("") == ()
    assert parse_dims("[]") == () and parse_dims(" [ prompt: int, sample: str, ] ") == dims
    with pytest.raises(ArrayTypeError, match="Unclosed"):
        parse_dims("[a: int")
    assert as_dims({"a": "int"}) == (Dim("a", "int"),)
    assert as_dims(dims) == dims
    for bad in ["a: float", "a: int?", "a: list[int]", "a: int, a: int", "a int"]:
        with pytest.raises(ArrayTypeError):
            parse_dims(bad)
    with pytest.raises(ArrayTypeError):
        Dim("", "int")
    with pytest.raises(ArrayTypeError):
        Dim("a", "float")  # type: ignore[arg-type]


def test_union_dims_matches_by_name() -> None:
    a, b, c = Dim("a", "int"), Dim("b", "str"), Dim("c", "int")
    assert union_dims([(a, b), (c, a)]) == (a, b, c)
    with pytest.raises(ArrayTypeError):
        union_dims([(a,), (Dim("a", "str"),)])


@pytest.mark.parametrize(
    "ttype",
    [
        ArrayType.of(int),
        ArrayType.of(("[a: int]", str | None)),
        ArrayType.of(("[a: int, b: str]", struct_of(x=int))),
    ],
)
def test_array_type_round_trips_through_the_wire(ttype: ArrayType) -> None:
    assert ArrayType.from_wire(ttype.to_wire()) == ttype


def test_array_type_conveniences() -> None:
    ttype = ArrayType.of(("[a: int, b: str]", int))
    assert ArrayType.of(int) == ArrayType((), LeafSchema("int"))
    assert ArrayType.of(({"a": "int", "b": "str"}, int)) == ttype
    assert ArrayType.of(ttype) is ttype
    assert ttype.dim_names == ("a", "b")
    assert ttype.dim("b") == Dim("b", "str")
    assert ttype.without(["a", "zzz"]) == ArrayType.of(("[b: str]", int))
    # str is for display only.
    assert str(ttype) == "[a: int, b: str]: int"
    assert str(ArrayType.of(("[a: int]", str | None))) == "[a: int]: str?"
    assert str(ArrayType.of(int)) == "[]: int"
    with pytest.raises(KeyError):
        ttype.dim("zzz")
    # An array type has no text form, and both of its parts are validated.
    with pytest.raises(ArrayTypeError, match="not written as a string"):
        ArrayType.of("[a: int]: int")
    for bad in [("[a: int int", int), ("[a: float]", int), ("[a: int]", dict[str, int])]:
        with pytest.raises(ArrayTypeError):
            ArrayType.of(bad)
    # The constructor validates a Dims tuple; .of is the only spec-normalizing entry point.
    assert ArrayType(as_dims("a: int"), LeafSchema("int")) == ArrayType.of(("[a: int]", int))
    with pytest.raises(ArrayTypeError, match="Expected a Dim"):
        ArrayType("a: int", LeafSchema("int"))  # type: ignore[arg-type]
    with pytest.raises(ArrayTypeError):
        ArrayType.from_wire({"dims": [["a", "int"]]})
    with pytest.raises(ArrayTypeError):
        ArrayType.from_wire({"dims": [["a", "int", "x"]], "schema": "int"})


def test_partial_array_type_accepts_dims_only_and_full_specs() -> None:
    assert PartialArrayType.of("[a: int]") == PartialArrayType(as_dims("a: int"))
    assert PartialArrayType.of(("[a: int]", str)) == PartialArrayType(
        as_dims("a: int"), LeafSchema("str")
    )
    assert PartialArrayType.of(str) == PartialArrayType((), LeafSchema("str"))
    assert str(PartialArrayType.of("[a: int, b: str]")) == "[a: int, b: str]"
    assert str(PartialArrayType.of(("[a: int]", str))) == "[a: int]: str"
    ttype = ArrayType.of(("[a: int]", str))
    assert PartialArrayType.of(ttype) == PartialArrayType.of(("[a: int]", str))
    assert PartialArrayType.of(({"a": "int"}, None)) == PartialArrayType.of("[a: int]")
    assert PartialArrayType.of(ttype).complete() == ttype
    # A dims string alone is a partial type, never an array type.
    with pytest.raises(ArrayTypeError, match="not written as a string"):
        ArrayType.of("[a: int]")
    with pytest.raises(ArrayTypeError, match="value schema"):
        PartialArrayType.of("[a: int]").complete()
    with pytest.raises(ArrayTypeError):
        PartialArrayType((), "int")  # type: ignore[arg-type]
    # Like ArrayType, the constructor validates a Dims tuple rather than normalizing a spec.
    with pytest.raises(ArrayTypeError, match="Expected a Dim"):
        PartialArrayType("a: int")  # type: ignore[arg-type]


def test_partial_array_type_matches_dims_in_order() -> None:
    ttype = ArrayType.of(("[a: int, b: str]", int))
    assert PartialArrayType.of("[a: int, b: str]").matches(ttype)
    assert PartialArrayType.of(("[a: int, b: str]", int)).matches(ttype)
    # Stricter than assignability: dim order counts, and the schema must be equal, not assignable.
    assert is_assignable_array(ttype, ArrayType.of(("[b: str, a: int]", int | None)))
    assert not PartialArrayType.of("[b: str, a: int]").matches(ttype)
    assert not PartialArrayType.of(("[a: int, b: str]", int | None)).matches(ttype)
    assert not PartialArrayType.of("[a: int]").matches(ttype)
    with pytest.raises(ArrayTypeError, match=r"Array: expected \[a: int\], got \[a: int, b: str\]"):
        PartialArrayType.of("[a: int]").check(ttype)


def test_expect_type_checks_and_returns_the_array() -> None:
    array = Array.from_items([((1,), "x")], ("[a: int]", str))
    assert array.expect_type("[a: int]") is array
    assert array.expect_type(("[a: int]", str)) == array
    with pytest.raises(ArrayTypeError, match="expected"):
        array.expect_type("[b: int]")
    with pytest.raises(ArrayTypeError, match="expected"):
        array.expect_type(("[a: int]", int))


class Entry(TypedDict):
    who: BoundID[Leaf]
    tags: list[str]


async def test_typed_views_present_values_as_annotated() -> None:
    storage = MemoryStorage()
    ref = await storage.store(Leaf(label="x"))
    array = Array.from_items([((1,), {"who": ref, "tags": ["a"]})], ("[id: int]", Entry))
    # Rows are stored canonically: bare ids, tuples, frozendicts.
    frozen = frozendict({"tags": ("a",), "who": ref.entity_id})
    assert array.raw_rows == (((1,), frozen),)
    assert list(array.raw_values()) == [frozen] and list(array.raw_items()) == [((1,), frozen)]
    assert next(array.raw_records()) == frozendict({"id": 1, **frozen})
    # Accessors present: a fresh dict of the TypedDict's shape, bound ids, lists.
    row = array[(1,)]
    assert type(row) is dict and row == {"who": ref, "tags": ["a"]}
    assert row["who"].loader is ref.loader and type(row["tags"]) is list
    assert array[(1,)] is not row  # a fresh copy each time
    assert array.get((1,)) == row and array.get((2,)) is None
    assert list(array.values()) == [row] and list(array.items()) == [((1,), row)]
    assert array.rows == (((1,), row),)
    assert next(array.records()) == frozendict({"id": 1, "who": ref, "tags": ["a"]})
    # Presentation is not part of equality or identity.
    plain = Array.from_items(
        [((1,), {"who": ref.entity_id, "tags": ["a"]})],
        ("[id: int]", struct_of(who=id_of(LEAF_TYPE), tags=list_of(str))),
    )
    assert plain == array and hash(plain) == hash(array)
    assert (await storage.store(plain)).entity_id == (await storage.store(array)).entity_id
    assert plain[(1,)] == frozen and plain.rows is plain.raw_rows
    # A scalar id with a loader presents bound; without one, bare.
    assert Array.scalar(ref, id_of(Leaf)).item() == ref
    assert Array.scalar(ref, IdSchema(LEAF_TYPE)).item() == ref.entity_id
    # Operations keep the presentation.
    assert (await array.field("who").collect())[(1,)] == ref
    assert (await array.rename({"id": "k"}).collect())[(1,)] == row
    assert Array.stack({0: array, 1: array}, "run")[(1, 1)] == row


async def test_retype_adopts_the_declared_type() -> None:
    storage = MemoryStorage()
    ref = await storage.store(Leaf(label="x"))
    array = Array.from_items([((1, "a"), ref.entity_id)], ("[a: int, b: str]", id_of(LEAF_TYPE)))
    # Same data, declared with a loader and in another dim order.
    typed = array.retype(("[b: str, a: int]", BoundID[Leaf]))
    assert typed.dims == parse_dims("b: str, a: int")
    assert typed.raw_rows == ((("a", 1), ref.entity_id),) and typed[("a", 1)] == ref
    assert typed.retype(array.type) == array
    # A looser declaration is adopted as declared.
    assert array.retype(("[a: int, b: str]", EntityID)).schema == IdSchema()
    assert array.retype(("[a: int, b: str]", EntityID | None)).schema == NullableSchema(IdSchema())
    with pytest.raises(ArrayTypeError, match="Cannot retype"):
        array.retype(("[a: int, b: str]", int))
    with pytest.raises(ArrayTypeError, match="Cannot retype"):
        array.retype(("[a: int]", EntityID))
    with pytest.raises(ArrayTypeError, match="Cannot retype"):
        array.retype(("[a: int, b: str]", BoundID[Holder]))  # a different type id
    # expect_type checks equality (dims in order) and then adopts.
    expected = array.expect_type(("[a: int, b: str]", BoundID[Leaf]))
    assert expected == array and expected[(1, "a")] == ref
    with pytest.raises(ArrayTypeError, match="expected"):
        array.expect_type(("[b: str, a: int]", BoundID[Leaf]))


async def test_loading_adopts_the_annotated_schema() -> None:
    storage = MemoryStorage()
    ref = await storage.store(Leaf(label="x"))
    plain = Array.from_items(
        [((1,), {"who": ref.entity_id, "tags": ["a"]})],
        ("[id: int]", struct_of(who=id_of(LEAF_TYPE), tags=list_of(str))),
    )
    stored = await storage.store(plain)
    # No annotation, a bare Array, Array[Any], or the FrozenValue default: as stored.
    for annotation in (Array, Array[Any], Array[FrozenValue]):
        loaded = await storage.load(stored, as_type=annotation)
        assert loaded == plain and loaded[(1,)] == plain[(1,)]
    assert (await storage.load(stored))[(1,)] == plain[(1,)]
    # Array[X]: the stored schema is checked to be assignable to X's, then X's is adopted.
    typed = await storage.load(stored, as_type=Array[Entry])
    assert typed == plain and typed[(1,)] == {"who": ref, "tags": ["a"]}
    assert typed.schema.python_type is Entry  # type: ignore[union-attr]
    looser = await storage.load(stored, as_type=Array[Loose])
    assert looser[(1,)] == {"who": ref.entity_id, "tags": ("a",)}
    assert looser.schema.fields["who"] == IdSchema()  # type: ignore[union-attr]
    with pytest.raises(ConversionError, match="not assignable"):
        await storage.load(stored, as_type=Array[int])
    with pytest.raises(ConversionError, match="no fixed fields"):
        await storage.load(stored, as_type=Array[dict[str, Any]])
    # Re-storing a presented array gives the same id: presentation is off the wire.
    assert (await storage.store(typed)).entity_id == stored.entity_id


class Loose(TypedDict):
    who: EntityID
    tags: Sequence[str]


def test_is_assignable_array_ignores_dim_order() -> None:
    a = ArrayType.of(("[a: int, b: str]", int))
    assert is_assignable_array(a, ArrayType.of(("[b: str, a: int]", int | None)))
    assert not is_assignable_array(a, ArrayType.of(("[a: int]", int)))
    assert not is_assignable_array(a, ArrayType.of(("[a: str, b: str]", int)))
    assert not is_assignable_array(a, ArrayType.of(("[a: int, b: str]", str)))


def test_static_operation_types() -> None:
    inner = struct_of(x=int, y=optional(str))
    t = ArrayType.of(("[a: int, b: str]", inner))
    assert field_type(t, "x") == ArrayType.of(("[a: int, b: str]", int))
    nullable = ArrayType.of(("[a: int]", optional(inner)))
    assert field_type(nullable, "x") == ArrayType.of(("[a: int]", int | None))
    assert field_type(nullable, "y") == ArrayType.of(("[a: int]", str | None))
    with pytest.raises(ArrayTypeError):
        field_type(t, "zzz")
    with pytest.raises(ArrayTypeError):
        field_type(ArrayType.of(("[a: int]", int)), "x")

    assert sel_type(t, ["b"]) == ArrayType.of(("[a: int]", inner))
    with pytest.raises(ArrayTypeError):
        sel_type(t, ["zzz"])

    assert combine_type(
        {
            "p": ArrayType.of(("[a: int]", str)),
            "q": ArrayType.of(("[b: str, a: int]", int)),
        }
    ) == ArrayType.of(("[a: int, b: str]", struct_of(p=str, q=int)))
    with pytest.raises(ArrayTypeError):
        combine_type({"p": ArrayType.of(("[a: int]", str)), "q": ArrayType.of(("[a: str]", int))})

    s = Dim("s", "str")
    assert stack_type(
        [ArrayType.of(("[a: int, b: str]", int)), ArrayType.of(("[b: str, a: int]", int))], s
    ) == ArrayType.of(("[s: str, a: int, b: str]", int))
    with pytest.raises(ArrayTypeError):
        stack_type([], s)
    with pytest.raises(ArrayTypeError):
        stack_type([ArrayType.of(("[a: int]", int)), ArrayType.of(("[a: int]", int | None))], s)
    with pytest.raises(ArrayTypeError):
        stack_type([ArrayType.of(("[s: str]", int))], s)


# ---- construction ----


def test_rows_are_canonical_sorted_frozen() -> None:
    array = make_array()
    assert array.type == ArrayType.of(TYPE)
    assert array.rows == (
        ((1, "a"), frozendict({"score": 1.5, "tags": ("z",)})),
        ((1, "b"), frozendict({"score": 0.5, "tags": ()})),
        ((2, "a"), frozendict({"score": None, "tags": ("x", "y")})),
    )
    assert array == Array.from_records(reversed(RECORDS), TYPE)
    assert hash(array) == hash(Array.from_records(reversed(RECORDS), TYPE))
    assert array == Array(
        dims=array.dims, schema=array.schema, raw_rows=tuple(reversed(array.raw_rows))
    )
    # Dim order is significant for equality, though every operation matches dims by name.
    assert array != Array.from_records(RECORDS, ("split: str, id: int", SCHEMA))


def test_constructors() -> None:
    assert Array.scalar(3, int).rows == (((), 3),)
    assert Array.infer_scalar([1, None]).type == ArrayType.of(list_of(optional(int)))
    with pytest.raises(ArrayError):
        Array.infer_scalar([])
    by_mapping = Array.from_items(
        [({"split": "a", "id": 1}, 1.5), ({"id": 2, "split": "b"}, None)], (DIMS, float | None)
    )
    assert by_mapping.rows == (((1, "a"), 1.5), ((2, "b"), None))
    inferred = Array.infer_from_records(RECORDS, dims=["id", "split"])
    assert inferred == make_array()


def test_construction_validates() -> None:
    with pytest.raises(ArrayError, match="expected keys"):
        Array.from_records([{"id": 1, "split": "a", "score": 1.0}], TYPE)
    with pytest.raises(ArrayError, match="expected float"):
        Array.from_records([{"id": 1, "split": "a", "score": 1, "tags": []}], TYPE)
    with pytest.raises(ArrayError, match="Duplicate key"):
        Array.from_records([RECORDS[0], RECORDS[0]], TYPE)
    with pytest.raises(ArrayError, match="expects int keys"):
        Array.from_items([(("1",), 1.0)], ("[id: int]", float))
    with pytest.raises(ArrayError, match="expected 1 key values"):
        Array.from_items([((1, 2), 1.0)], ("[id: int]", float))
    with pytest.raises(ArrayError, match="expected dims"):
        Array.from_items([({"nope": 1}, 1.0)], ("[id: int]", float))
    with pytest.raises(ArrayTypeError):
        Array.from_records(RECORDS, (DIMS, int))
    with pytest.raises(ArrayError, match="exactly one value"):
        Array(dims=(), schema=LeafSchema("int"), raw_rows=())
    with pytest.raises(ArrayError, match="Duplicate key"):
        Array.from_items([((), 1), ((), 2)], ("[]", int))


def test_infer_from_items() -> None:
    # Mapping keys: dim order comes from the first key.
    array = Array.infer_from_items(
        [({"split": "a", "id": 2}, [1, None]), ({"id": 1, "split": "b"}, [])]
    )
    assert array.type == ArrayType.of(("[split: str, id: int]", list_of(optional(int))))
    assert array.rows == ((("a", 2), (1, None)), (("b", 1), ()))
    # Tuple keys need explicit dim names; kinds are still inferred.
    by_tuple = Array.infer_from_items([((1,), "x"), ((2,), "y")], dims=["id"])
    assert by_tuple.type == ArrayType.of(("[id: int]", str))
    assert Array.infer_from_items([((), 3)], dims=[]).item() == 3
    with pytest.raises(ArrayError, match="zero items"):
        Array.infer_from_items([])
    with pytest.raises(ArrayError, match="dims are required"):
        Array.infer_from_items([((1,), "x")])
    with pytest.raises(ArrayError, match="expected dims"):
        Array.infer_from_items([({"id": 1}, "x"), ({"other": 1}, "y")])
    with pytest.raises(ArrayError, match="expected 1 key values"):
        Array.infer_from_items([((1, 2), "x")], dims=["id"])
    with pytest.raises(ArrayTypeError):  # a float key is not a key kind
        Array.infer_from_items([((1.5,), "x")], dims=["id"])  # type: ignore[list-item]
    with pytest.raises(ArrayError):  # conflicting value schemas
        Array.infer_from_items([({"id": 1}, "x"), ({"id": 2}, 3)])


def test_infer_from_records_validates() -> None:
    with pytest.raises(ArrayError):
        Array.infer_from_records([], dims=["id"])
    with pytest.raises(ArrayError):
        Array.infer_from_records([{"id": 1}], dims=["missing"])
    with pytest.raises(ArrayError):
        Array.infer_from_records([{"id": 1, "x": 1}, {"other": 1, "x": 1}], dims=["id"])
    with pytest.raises(ArrayTypeError):  # an inferred nullable dim kind is not a key kind
        Array.infer_from_records([{"id": None, "x": 1}, {"id": 1, "x": 2}], dims=["id"])
    with pytest.raises(ArrayError):  # undetermined field schema
        Array.infer_from_records([{"id": 1, "x": None}], dims=["id"])


# ---- access ----


def test_mapping_style_access() -> None:
    array = make_array()
    assert len(array) == 3
    assert array.dim_names == ("id", "split")
    assert list(array) == [(1, "a"), (1, "b"), (2, "a")]
    assert list(array.key_records())[0] == frozendict({"id": 1, "split": "a"})
    assert array[{"id": 1, "split": "b"}] == frozendict({"score": 0.5, "tags": ()})
    assert array[(1, "b")] == array[{"split": "b", "id": 1}]
    assert (2, "a") in array
    assert {"id": 3, "split": "a"} not in array
    assert {"id": 1} not in array
    assert array.get((9, "z")) is None
    assert list(array.values())[0] == frozendict({"score": 1.5, "tags": ("z",)})
    assert list(array.items())[1] == ((1, "b"), frozendict({"score": 0.5, "tags": ()}))
    assert list(array.records())[2] == frozendict(
        {"id": 2, "split": "a", "score": None, "tags": ("x", "y")}
    )
    with pytest.raises(KeyError):
        array[(1,)]
    with pytest.raises(KeyError):
        array[{"id": 1, "nope": "b"}]
    with pytest.raises(KeyError):
        array[(3, "a")]
    with pytest.raises(ArrayTypeError):
        array.item()
    with pytest.raises(ArrayTypeError):
        list(scores((1, 1.0)).records())


def test_scalar_item() -> None:
    assert Array.scalar("x", str).item() == "x"
    assert Array.scalar("x", str)[()] == "x"


# ---- operations ----


async def test_sel() -> None:
    array = make_array()
    sliced = await array.sel({"split": "a"}).collect()
    assert sliced.type == ArrayType.of(("[id: int]", SCHEMA))
    assert list(sliced.keys()) == [(1,), (2,)]
    assert sliced[(2,)] == array[(2, "a")]
    assert (await array.sel({"id": 1, "split": "b"}).collect()).item() == array[(1, "b")]
    assert len(await array.sel({"id": 9}).collect()) == 0
    with pytest.raises(ArrayTypeError):
        array.sel({"nope": 1})
    with pytest.raises(ArrayError, match="exactly one"):
        await array.sel({"id": 9, "split": "a"}).collect()


async def test_rename() -> None:
    array = make_array()
    renamed = await array.rename({"id": "prompt"}).collect()
    assert renamed.type == ArrayType.of(("[prompt: int, split: str]", SCHEMA))
    assert renamed.rows == array.rows
    swapped = await array.rename({"id": "split", "split": "id"}).collect()
    assert swapped.dim_names == ("split", "id")
    assert rename_type(array.type, {}) == array.type
    with pytest.raises(ArrayTypeError):
        array.rename({"nope": "x"})
    with pytest.raises(ArrayTypeError):
        array.rename({"id": "split"})


async def test_field() -> None:
    array = make_array()
    score = await array.field("score").collect()
    assert score.type == ArrayType.of((DIMS, float | None))
    assert score.rows == (((1, "a"), 1.5), ((1, "b"), 0.5), ((2, "a"), None))
    nullable = Array.from_items(
        [((1,), {"x": 1}), ((2,), None)], ("[id: int]", optional(struct_of(x=int)))
    )
    assert (await nullable.field("x").collect()).rows == (((1,), 1), ((2,), None))
    with pytest.raises(ArrayTypeError):
        array.field("nope")


def test_join_keys_exact_and_inner_and_broadcast() -> None:
    a = scores((1, 1.0), (2, 2.0))
    b = scores((2, 2.0), (3, 3.0))
    c = Array.from_items([(("x",), 1), (("y",), 2)], ("[s: str]", int))
    unit = Array.scalar(0, int)
    dims = as_dims("id: int, s: str")

    assert join_keys({"a": a, "c": c, "u": unit}, dims) == [(1, "x"), (1, "y"), (2, "x"), (2, "y")]
    assert join_keys({"a": a, "b": b}, dims[:1], "inner") == [(2,)]
    with pytest.raises(ArrayError, match="use join='inner'"):
        join_keys({"a": a, "b": b}, dims[:1])
    with pytest.raises(ArrayError, match="undetermined"):
        join_keys({"a": a}, dims)
    with pytest.raises(ArrayTypeError):
        join_keys({"a": a}, as_dims("id: str"))
    # An array with extra dims participates through its projection onto the target dims.
    wide = Array.from_items(
        [((1, "x"), 0), ((1, "y"), 0), ((2, "x"), 0)], ("[id: int, s: str]", int)
    )
    assert join_keys({"a": a, "w": wide}, dims[:1]) == [(1,), (2,)]
    # Exact only requires that no input's keys are dropped; a sparse array need not be a product.
    assert join_keys({"w": wide, "c": c}, dims) == [(1, "x"), (1, "y"), (2, "x")]
    with pytest.raises(ArrayError, match="use join='inner'"):
        join_keys({"w": wide, "c": Array.from_items([(("x",), 1)], ("[s: str]", int))}, dims)


def test_combine() -> None:
    a = scores((1, 1.0), (2, 2.0))
    names = Array.from_items([((2,), "two"), ((1,), "one")], ("[id: int]", str))
    unit = Array.scalar("k", str)
    combined = Array.combine({"score": a, "name": names, "k": unit})
    assert combined.type == ArrayType.of(("[id: int]", struct_of(k=str, name=str, score=float)))
    assert combined[(1,)] == frozendict({"score": 1.0, "name": "one", "k": "k"})
    product = Array.combine({"a": a, "s": Array.from_items([(("x",), 1)], ("[s: str]", int))})
    assert list(product.keys()) == [(1, "x"), (2, "x")]
    with pytest.raises(ArrayError):
        Array.combine({"a": a, "b": scores((2, 2.0), (3, 3.0))})
    inner = Array.combine({"a": a, "b": scores((2, 2.0), (3, 3.0))}, join="inner")
    assert inner.rows == (((2,), frozendict({"a": 2.0, "b": 2.0})),)


def test_combine_outer() -> None:
    a = scores((1, 1.0), (2, 2.0))
    b = scores((2, 2.0), (3, 3.0))
    outer = Array.combine({"a": a, "b": b}, join="outer")
    assert outer.type == ArrayType.of(
        ("[id: int]", struct_of(a=optional(float), b=optional(float)))
    )
    assert outer.rows == (
        ((1,), frozendict({"a": 1.0, "b": None})),
        ((2,), frozendict({"a": 2.0, "b": 2.0})),
        ((3,), frozendict({"a": None, "b": 3.0})),
    )
    # A lower-dim input is broadcast along the union of labels on the dims it lacks; an input that
    # is sparse in those dims gets None where it has no key.
    wide = Array.from_items([((1, "x"), 0), ((2, "y"), 0)], ("[id: int, s: str]", int))
    c = Array.from_items([(("x",), 1), (("z",), 2)], ("[s: str]", int | None))
    combined = Array.combine({"w": wide, "c": c}, join="outer")
    assert combined.type == ArrayType.of(
        ("[id: int, s: str]", struct_of(c=optional(int), w=optional(int)))
    )
    assert list(combined.keys()) == [(1, "x"), (1, "z"), (2, "x"), (2, "y"), (2, "z")]
    assert combined[(2, "y")] == frozendict({"w": 0, "c": None})
    assert combined[(1, "z")] == frozendict({"w": None, "c": 2})
    assert join_keys({"a": a, "u": Array.scalar(0, int)}, as_dims("id: int"), "outer") == [
        (1,),
        (2,),
    ]
    with pytest.raises(ArrayError, match="undetermined"):
        join_keys({"a": a}, as_dims("id: int, s: str"), "outer")


async def test_stack() -> None:
    a = scores((1, 1.0))
    b = Array.from_items([((1,), 2.0), ((2,), 3.0)], ("[id: int]", float))
    stacked = Array.stack({"a": a, "b": b}, dim="run")
    assert stacked.type == ArrayType.of(("[run: str, id: int]", float))
    assert stacked.rows == ((("a", 1), 1.0), (("b", 1), 2.0), (("b", 2), 3.0))
    assert await stacked.sel({"run": "b"}).collect() == b
    by_int = Array.stack({7: Array.scalar("x", str)}, dim="n")
    assert by_int.type == ArrayType.of(("[n: int]", str))
    with pytest.raises(ArrayTypeError):
        Array.stack({"a": a, 1: b}, dim="run")  # type: ignore[dict-item]
    with pytest.raises(ArrayTypeError):
        Array.stack({}, dim="run")
    with pytest.raises(ArrayTypeError):
        Array.stack({"a": a, "b": Array.scalar(1.0, float)}, dim="run")
    with pytest.raises(ArrayTypeError):
        Array.stack({"a": a}, dim="id")


async def test_stack_over_and_transpose() -> None:
    a = scores((1, 1.0))
    b = Array.from_items([((1,), 2.0), ((2,), 3.0)], ("[id: int]", float))
    dims = parse_dims("run: str, seed: int")
    stacked = Array.stack_over({("x", 0): a, ("x", 1): b, ("y", 0): b}, dims)
    assert stacked.type == ArrayType.of(("[run: str, seed: int, id: int]", float))
    assert await stacked.sel({"run": "x", "seed": 1}).collect() == b
    assert await stacked.sel({"run": "y"}).collect() == Array.stack({0: b}, dim="seed")
    assert stack_over_type([a.type], dims) == stacked.type
    # Dims of the inputs are matched by name, whatever their order.
    wide = Array.from_items([((1, "p"), 1.0)], ("[id: int, s: str]", float))
    flipped = wide.transpose(["s", "id"])
    assert flipped.type == ArrayType.of(("[s: str, id: int]", float))
    assert flipped.rows == ((("p", 1), 1.0),)
    assert flipped.transpose(["id", "s"]) == wide
    assert Array.stack_over({(0,): wide, (1,): flipped}, parse_dims("k: int")).rows == (
        ((0, 1, "p"), 1.0),
        ((1, 1, "p"), 1.0),
    )
    with pytest.raises(ArrayTypeError, match="permutation"):
        wide.transpose(["id"])
    with pytest.raises(ArrayTypeError, match="collide"):
        Array.stack_over({(1,): a}, parse_dims("id: int"))
    with pytest.raises(ArrayError, match="expected 2 key values"):
        Array.stack_over({("x",): a}, dims)
    with pytest.raises(ArrayError, match="expects int keys"):
        Array.stack_over({("x", "0"): a}, dims)
    with pytest.raises(ArrayTypeError):
        Array.stack_over({}, dims)


# ---- serialization ----


def test_wire_form() -> None:
    data = DEFAULT_CONVERTER.unstructure_entity(make_array())
    assert data["$type"] == ARRAY_TYPE == "org.transluce.fxtr.core.Array"
    assert data["dims"] == [["id", "int"], ["split", "str"]]
    assert data["schema"] == {"struct": {"score": {"nullable": "float"}, "tags": {"list": "str"}}}
    assert data["rows"] == [
        [1, "a", {"score": 1.5, "tags": ["z"]}],
        [1, "b", {"score": 0.5, "tags": []}],
        [2, "a", {"score": None, "tags": ["x", "y"]}],
    ]
    assert DEFAULT_CONVERTER.unstructure_entity(Array.scalar(3, int))["rows"] == [[3]]


async def test_store_load_round_trip_with_nested_and_ref_values() -> None:
    storage = MemoryStorage()
    leaf_ref = await storage.store(Leaf(label="x"))
    array = Array.from_records(
        [
            {"id": 1, "who": leaf_ref.entity_id, "meta": {"a": [1, None], "b": b"\x00"}},
            {"id": 0, "who": leaf_ref.entity_id, "meta": None},
        ],
        (
            "[id: int]",
            struct_of(who=EntityID, meta=optional(struct_of(a=list_of(optional(int)), b=bytes))),
        ),
    )
    ref = await storage.store(array)
    loaded = await storage.load(ref)
    assert loaded == array
    assert loaded[(1,)]["meta"] == frozendict({"a": (1, None), "b": b"\x00"})  # type: ignore[index]
    assert (await storage.store(loaded)).entity_id == ref.entity_id


async def test_typed_id_schema_round_trips() -> None:
    storage = MemoryStorage()
    leaf_ref = await storage.store(Leaf(label="x"))
    marker = "test.fxtr.array.marker"  # a type id no class in this test declares
    array = Array.from_items([((1,), leaf_ref.entity_id)], ("[id: int]", id_of(marker)))
    assert array.schema == IdSchema(marker)
    ref = await storage.store(array)
    loaded = await storage.load(ref)
    assert loaded == array
    assert loaded.schema == IdSchema(marker)
    assert str(loaded.schema) == str(array.schema) == "id[test.fxtr.array.marker]"
    assert is_assignable_array(loaded.type, array.type)
    assert is_assignable_array(loaded.type, ArrayType.of(("[id: int]", EntityID)))
    assert not is_assignable_array(ArrayType.of(("[id: int]", EntityID)), array.type)
    assert stack_type([loaded.type, array.type], Dim("run", "int")) == ArrayType.of(
        ("[run: int, id: int]", id_of(marker))
    )
    assert (await storage.store(loaded)).entity_id == ref.entity_id
    assert Array.fxtr_type_id() == ARRAY_TYPE


async def test_construction_order_does_not_affect_content_id() -> None:
    storage = MemoryStorage()
    a = await storage.store(make_array())
    b = await storage.store(Array.from_records(list(reversed(RECORDS)), TYPE))
    assert a.entity_id == b.entity_id
    reordered = struct_of(tags=list_of(str), score=optional(float))
    assert (
        await storage.store(Array.from_records(RECORDS, (DIMS, reordered)))
    ).entity_id == a.entity_id
    assert make_array().content_id() == a.entity_id


async def test_array_ref_field_in_dataclass_entity() -> None:
    storage = MemoryStorage()
    narrow: Array[Literal["one"]] = Array.from_items([((1,), "one")], ("[id: int]", str))
    array: Array[str] = narrow  # V is covariant, so a narrower value type widens
    holder = Holder(array=await storage.store(array))
    loaded = await storage.load(await storage.store(holder))
    assert await storage.load(loaded.array) == array


@dataclasses.dataclass(frozen=True, kw_only=True)
class TypeHolder(DataclassEntity, fxtr_type="test.fxtr.array.typeHolder"):
    ttype: ArrayType


def test_array_type_field_in_dataclass_entity() -> None:
    # An ArrayType is a value type: as a field it serializes as its wire form, and malformed data
    # is rejected strictly.
    holder = TypeHolder(ttype=ArrayType.of(("[prompt: int]", struct_of(x=int, y=optional(str)))))
    data = DEFAULT_CONVERTER.unstructure_entity(holder)
    assert data["ttype"] == holder.ttype.to_wire()
    assert DEFAULT_CONVERTER.structure(data, TypeHolder) == holder
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure({**data, "ttype": {"dims": []}}, TypeHolder)


def test_load_rejects_non_canonical_or_malformed_data() -> None:
    good = DEFAULT_CONVERTER.unstructure_entity(make_array())
    rows = good["rows"]
    assert isinstance(rows, list)
    with pytest.raises(ConversionError, match="canonical"):
        DEFAULT_CONVERTER.structure({**good, "rows": list(reversed(rows))}, Array)
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure({**good, "extra": 1}, Array)
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure({**good, "rows": [[1, "a"]]}, Array)
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure({**good, "rows": [[1, "a", {"score": 1, "tags": []}]]}, Array)
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure({**good, "dims": [["id", "float"], ["split", "str"]]}, Array)
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure({**good, "schema": "int??"}, Array)
