"""An array built from Arrow and the same array built from Python rows are the same array."""

from __future__ import annotations

import copy
import pickle
import threading
from collections.abc import Callable
from typing import Any, TypedDict, cast

import pyarrow as pa
import pytest
from array_testing import cid_for, identity_arrays

from fxtr.core import codec
from fxtr.core.entity_data import EntityID, UnknownReferenceError
from fxtr.entity_defns.array import Array, ArrayError, ArrayType, field_type
from fxtr.entity_defns.arrow import (
    ArrowArrayError,
    array_arrow_schema,
    array_from_canonical_data,
    rows_to_arrow_batches,
)
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.schema import IdSchema, list_of, optional, struct_of
from fxtr.storage.memory_storage import MemoryStorage

ARRAYS = identity_arrays()


def _from_arrow(array: Array[Any], batch_size: int = 65_536) -> Array[Any]:
    """The same array, built from an Arrow table with no Python rows."""
    table = pa.Table.from_batches(
        list(rows_to_arrow_batches(array.raw_rows, array.type, batch_size=batch_size)),
        schema=array_arrow_schema(array.type),
    )
    built = Array._from_arrow(table, array.type)
    assert not built._backing.has_rows
    return built


@pytest.mark.parametrize("array", ARRAYS, ids=lambda array: str(array.type))
@pytest.mark.parametrize("batch_size", [1, 2, 65_536])
def test_an_arrow_built_array_reads_like_the_python_built_one(
    array: Array[Any], batch_size: int
) -> None:
    built = _from_arrow(array, batch_size)

    assert built.type == array.type
    assert len(built) == len(array)
    assert list(built.keys()) == list(array.keys())
    for key in array.keys():
        assert key in built
        assert built[key] == array[key]
        assert built.row_position(key) == array.row_position(key)
    assert list(built.items()) == list(array.items())
    assert list(built.raw_values()) == list(array.raw_values())
    if not array.dims:
        assert built.item() == array.item()
    # Reading row by row decodes a batch at a time, and never keeps the whole tuple.
    assert not built._backing.has_rows
    assert built.raw_rows == array.raw_rows
    assert built._backing.has_rows
    assert built._backing.python_materializations == 1


@pytest.mark.parametrize("array", ARRAYS, ids=lambda array: str(array.type))
def test_an_arrow_built_array_serializes_and_compares_as_the_python_built_one(
    array: Array[Any],
) -> None:
    built = _from_arrow(array)

    assert built == array and array == built
    assert hash(built) == hash(array)
    assert codec.encode(DEFAULT_CONVERTER.unstructure_entity(built)) == codec.encode(
        DEFAULT_CONVERTER.unstructure_entity(array)
    )
    assert built.content_id() == array.content_id()


def test_equality_is_python_value_equality_whichever_form_is_held() -> None:
    zero = Array.from_items([((1,), 0.0)], ("[i: int]", float))
    negative_zero = Array.from_items([((1,), -0.0)], ("[i: int]", float))
    different = Array.from_items([((1,), 1.0)], ("[i: int]", float))

    for left_form in (zero, _from_arrow(zero)):
        for right_form in (negative_zero, _from_arrow(negative_zero)):
            assert left_form == right_form
            assert hash(left_form) == hash(right_form)
    assert zero != different and _from_arrow(zero) != _from_arrow(different)


def test_comparing_arrow_built_arrays_keeps_no_rows() -> None:
    array = Array.from_items([((i,), i) for i in range(10_000)], ("[i: int]", int))
    left, right = _from_arrow(array), _from_arrow(array)

    assert left == right
    assert hash(left) == hash(right) == hash(array)
    assert not left._backing.has_rows and not right._backing.has_rows


@pytest.mark.parametrize("array", ARRAYS, ids=lambda array: str(array.type))
def test_each_form_copies_and_pickles_as_itself(array: Array[Any]) -> None:
    built = _from_arrow(array)
    for original in (array, built):
        assert copy.copy(original) is original
        assert copy.deepcopy(original) is original
        restored = pickle.loads(pickle.dumps(original))
        assert restored == original
        # The form an array was built in is what travels; nothing else is forced.
        assert restored._backing.has_rows == original._backing.has_rows
        assert restored._backing.has_table == original._backing.has_table


def test_a_python_built_array_never_converts_unless_asked() -> None:
    array = ARRAYS[1]
    list(array.items())
    _ = array[(4,)], len(array), array.content_id(), array == array, hash(array)
    pickle.loads(pickle.dumps(array))
    assert not array._backing.has_table
    assert array._backing.arrow_conversions == 0


RUNS = Array.from_items(
    [
        (("b", 2), {"score": 0.5, "tags": ("x",), "meta": {"ok": True}}),
        (("a", 2), {"score": -0.0, "tags": (), "meta": None}),
        (("a", 1), {"score": 1.5, "tags": ("y", "z"), "meta": {"ok": False}}),
        (("é", 1), {"score": 2.0, "tags": ("w",), "meta": {"ok": True}}),
    ],
    (
        "[model: str, sample: int]",
        struct_of(score=float, tags=list_of(str), meta=optional(struct_of(ok=bool))),
    ),
)
NULLABLE_STRUCTS = Array.from_items(
    [((1,), {"x": 5, "inner": {"y": "a"}}), ((2,), None)],
    ("[i: int]", optional(struct_of(x=int, inner=struct_of(y=str)))),
)
WITH_IDS = Array.from_items(
    [
        ((1,), {"ref": EntityID(cid=cid_for(7)), "n": 1}),
        ((2,), {"ref": EntityID(cid=cid_for(8)), "n": 2}),
    ],
    ("[i: int]", struct_of(ref=IdSchema(), n=int)),
)
MODEL_LABELS = Array.from_items(
    [(("a",), "first"), (("b",), "second"), (("z",), "absent")], ("[model: str]", str)
)

OPERATIONS: dict[str, tuple[Array[Any], Callable[[Array[Any]], Array[Any]]]] = {
    "transpose": (RUNS, lambda array: array.transpose(["sample", "model"])),
    "retype to an equal type": (RUNS, lambda array: array.retype(array.type)),
    "retype to a looser type": (
        RUNS,
        lambda array: array.retype(
            (
                "[sample: int, model: str]",
                optional(
                    struct_of(
                        score=optional(float),
                        tags=list_of(optional(str)),
                        meta=optional(struct_of(ok=optional(bool))),
                    )
                ),
            )
        ),
    ),
    "retype a nullable struct": (
        NULLABLE_STRUCTS,
        lambda array: array.retype(
            ("[i: int]", optional(struct_of(x=optional(int), inner=struct_of(y=str))))
        ),
    ),
    "strip presentation": (RUNS, lambda array: array.strip_presentation()),
    "stack": (RUNS, lambda array: Array.stack({"second": array, "first": array}, "run")),
    "combine with a Python array": (
        RUNS,
        lambda array: Array.combine({"run": array, "label": MODEL_LABELS}, join="inner"),
    ),
    "combine outer": (
        RUNS,
        lambda array: Array.combine({"run": array, "label": MODEL_LABELS}, join="outer"),
    ),
}


def _content_bytes(array: Array[Any]) -> bytes:
    """The array's canonical bytes."""
    return codec.encode(DEFAULT_CONVERTER.unstructure_entity(array))


@pytest.mark.parametrize("name", OPERATIONS)
def test_an_operation_on_an_arrow_built_array_matches_and_decodes_nothing(name: str) -> None:
    array, operation = OPERATIONS[name]
    expected = operation(array)
    built = _from_arrow(array, batch_size=2)

    result = operation(built)

    assert not built._backing.has_rows
    assert not result._backing.has_rows and result._backing.has_table
    assert result.type == expected.type
    assert list(result.keys()) == list(expected.keys())
    assert list(result.raw_items()) == list(expected.raw_items())
    assert result == expected and hash(result) == hash(expected)
    assert _content_bytes(result) == _content_bytes(expected)


@pytest.mark.anyio
async def test_a_scalar_sel_with_no_row_is_refused_in_either_form() -> None:
    for array in (RUNS, _from_arrow(RUNS)):
        with pytest.raises(ArrayError, match="exactly one"):
            await array.sel({"model": "none", "sample": 1}).collect()


class Labelled(TypedDict):
    label: str


def test_changing_only_presentation_shares_the_forms() -> None:
    presented = Array.from_items([((1,), {"label": "a"})], ("[i: int]", Labelled))
    for array in (presented, _from_arrow(presented)):
        stripped = array.strip_presentation()
        assert stripped.schema != array.schema or stripped.schema is not array.schema
        assert stripped._backing is array._backing
        assert stripped.retype(array.type)._backing is array._backing
        assert type(array[(1,)]) is dict


@pytest.mark.parametrize("array", ARRAYS, ids=lambda array: str(array.type))
def test_an_arrow_built_array_computes_its_content_id_from_its_table(array: Array[Any]) -> None:
    built = _from_arrow(array)

    assert built.content_id() == array.content_id()
    assert not built._backing.has_rows
    assert built._backing.content_id == built.content_id()


@pytest.mark.anyio
@pytest.mark.parametrize("array", ARRAYS, ids=lambda array: str(array.type))
@pytest.mark.parametrize("entry", ["async", "sync"])
async def test_storing_an_arrow_built_array_stores_the_same_entity(
    array: Array[Any], entry: str
) -> None:
    storage = MemoryStorage()
    # The entity the identity arrays' IDs name (see array_testing.cid_for).
    storage.store_raw_data_sync({"$type": "fxtr.builtin.test", "seed": 1})
    stored = storage.store_sync(array)
    built = _from_arrow(array)

    reference = await storage.store(built) if entry == "async" else storage.store_sync(built)

    assert reference.entity_id == stored.entity_id
    assert await storage.load(reference) == array


@pytest.mark.anyio
async def test_an_array_referring_to_an_unknown_entity_is_refused_in_either_form() -> None:
    unknown = Array.from_items([((1,), EntityID(cid=cid_for(9)))], ("[i: int]", IdSchema()))
    storage = MemoryStorage()

    for array in (unknown, _from_arrow(unknown)):
        with pytest.raises(UnknownReferenceError):
            await storage.store(array)


def _decoded(array: Array[Any]) -> dict[str, Any]:
    """An array's canonical data as a storage decodes it: plain lists and dicts, bare links."""
    return cast("dict[str, Any]", codec.decode(_content_bytes(array)))


def _decode(data: dict[str, Any], array_type: ArrayType) -> Array[Any]:
    return array_from_canonical_data(data, array_type)


@pytest.mark.parametrize("array", ARRAYS, ids=lambda array: str(array.type))
def test_canonical_data_decodes_straight_into_arrow(array: Array[Any]) -> None:
    decoded = array_from_canonical_data(_decoded(array), array.type)

    assert decoded is not None and not decoded._backing.has_rows
    assert decoded == array
    assert decoded.content_id() == array.content_id()


def _tampered(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    data = _decoded(RUNS)
    change(data)
    return data


def _swap_first_rows(data: dict[str, Any]) -> None:
    data["rows"][0], data["rows"][1] = data["rows"][1], data["rows"][0]


def _repeat_first_row(data: dict[str, Any]) -> None:
    data["rows"][1] = data["rows"][0]


def _set_score(score: object) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        data["rows"][0][2]["score"] = score

    return change


def _add_field(data: dict[str, Any]) -> None:
    data["rows"][0][2]["extra"] = 1


def _drop_key(data: dict[str, Any]) -> None:
    data["rows"][0] = data["rows"][0][1:]


def _null_key(data: dict[str, Any]) -> None:
    data["rows"][0][0] = None


def _entity_as_text(data: dict[str, Any]) -> None:
    data["rows"][0][2]["tags"] = [EntityID(cid=cid_for(1))]


def _bytes_as_text(data: dict[str, Any]) -> None:
    data["rows"][0][2]["tags"] = [b"y"]


def _int_as_bool(data: dict[str, Any]) -> None:
    data["rows"][0][2]["meta"] = {"ok": 1}


def _bool_as_int_key(data: dict[str, Any]) -> None:
    data["rows"][0][1] = True


@pytest.mark.parametrize(
    "change",
    [
        _swap_first_rows,
        _repeat_first_row,
        _set_score(1),
        _set_score(True),
        _set_score("1.5"),
        _set_score(None),
        _add_field,
        _drop_key,
        _null_key,
        _entity_as_text,
        _bytes_as_text,
        _int_as_bool,
        _bool_as_int_key,
    ],
    ids=lambda change: getattr(change, "__name__", "change"),
)
def test_noncanonical_or_malformed_data_is_refused(
    change: Callable[[dict[str, Any]], None],
) -> None:
    with pytest.raises(ArrowArrayError):
        _decode(_tampered(change), RUNS.type)


@pytest.mark.anyio
async def test_a_stored_array_loads_straight_into_arrow() -> None:
    storage = MemoryStorage()

    array = await storage.load(await storage.store(RUNS))

    assert not array._backing.has_rows
    assert array == RUNS and array.content_id() == RUNS.content_id()


def test_data_for_another_type_is_refused() -> None:
    with pytest.raises(ArrowArrayError, match="expected"):
        _decode(_decoded(RUNS), field_type(RUNS.type, "tags"))


def test_rows_are_decoded_once_however_many_threads_ask() -> None:
    array = _from_arrow(Array.from_items([((i,), i) for i in range(20_000)], ("[i: int]", int)))
    barrier = threading.Barrier(8)
    seen: list[object] = []

    def read() -> None:
        barrier.wait()
        seen.append(array.raw_rows)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(rows is seen[0] for rows in seen)
    assert array._backing.python_materializations == 1
