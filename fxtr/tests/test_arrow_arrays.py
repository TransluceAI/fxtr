from __future__ import annotations

from collections.abc import Callable

import pyarrow as pa
import pytest
from array_testing import cid_for, identity_arrays

from fxtr.core import codec
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.arrow import (
    ARROW_SCHEMA_VERSION,
    ArrowArrayError,
    array_arrow_schema,
    array_to_arrow_batches,
    array_type_from_arrow_schema,
    arrow_columns,
    arrow_table_content_id,
    arrow_table_to_array,
    normalize_arrow_batch,
    validate_arrow_batches,
)
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.schema import IdSchema, list_of, optional, struct_of


@pytest.mark.parametrize("array", identity_arrays(), ids=lambda array: str(array.type))
@pytest.mark.parametrize("batch_size", [1, 2, 65_536])
def test_an_arrow_table_has_exact_array_identity(array: Array[object], batch_size: int) -> None:
    batches = list(array_to_arrow_batches(array, batch_size=batch_size))
    table = pa.Table.from_batches(batches, schema=array_arrow_schema(array.type))

    expected = codec.encode(DEFAULT_CONVERTER.unstructure_entity(array))
    assert arrow_table_content_id(table, array.type).cid == codec.compute_cid(expected)
    assert arrow_table_to_array(table, array.type) == array


def test_arrow_schema_carries_version_type_and_unambiguous_columns() -> None:
    array = Array.from_items(
        [((1,), {"x": "field", "y": 2})],
        ("[x: int]", struct_of(x=str, y=int)),
    )

    schema = array_arrow_schema(array.type)
    columns = arrow_columns(array.type)

    assert ARROW_SCHEMA_VERSION == 3
    assert array_type_from_arrow_schema(schema) == array.type
    assert [column.role for column in columns] == ["dimension", "value", "value"]
    assert [column.logical_name for column in columns] == ["x", "x", "y"]
    assert len(set(schema.names)) == len(schema.names)


@pytest.mark.parametrize(
    ("array_factory", "message"),
    [
        (lambda: Array.scalar(2**63, int), "outside Arrow int64 range"),
        (lambda: Array.scalar(float("inf"), float), "cannot hold NaN"),
        (lambda: Array.scalar(EntityID(cid=b"short"), EntityID), "36-byte CID"),
    ],
)
def test_python_to_arrow_rejects_values_without_shared_canonical_representation(
    array_factory: Callable[[], Array[object]], message: str
) -> None:
    with pytest.raises(ArrowArrayError, match=message):
        list(array_to_arrow_batches(array_factory()))


def test_arrow_validation_rejects_duplicate_keys_across_batches() -> None:
    array = Array.from_items([((1,), "one"), ((2,), "two")], ("[i: int]", str))
    batches = list(array_to_arrow_batches(array, batch_size=1))
    duplicate = pa.RecordBatch.from_arrays(
        [batches[0].column(0), batches[1].column(1)],
        schema=batches[0].schema,
    )

    with pytest.raises(ArrowArrayError, match="duplicate.*batch boundaries"):
        validate_arrow_batches([batches[0], duplicate], array.type)


@pytest.mark.parametrize("sliced", [False, True])
def test_nested_list_normalization_preserves_nullable_structs_and_offsets(sliced: bool) -> None:
    schema = struct_of(
        events=list_of(
            struct_of(
                labels=list_of(str),
                metrics=optional(struct_of(quality=int)),
            )
        )
    )
    array = Array.from_items(
        [
            ((1842,), {"events": [{"labels": ["refund"], "metrics": {"quality": 95}}]}),
            ((2091,), {"events": [{"labels": [], "metrics": None}]}),
            ((3014,), {"events": []}),
        ],
        ("[ticket: int]", schema),
    )
    batch = next(array_to_arrow_batches(array))
    loose = pa.list_(
        pa.struct(
            [
                pa.field("labels", pa.list_(pa.string())),
                pa.field("metrics", pa.struct([pa.field("quality", pa.int64())])),
            ]
        )
    )
    result = pa.RecordBatch.from_arrays(
        [batch.column(0), pa.array(batch.column(1).to_pylist(), type=loose)],
        names=batch.schema.names,
    )
    if sliced:
        result = result.slice(1, 2)
        array = Array.from_items(array.raw_rows[1:], array.type)
    normalized = normalize_arrow_batch(result, array.type)
    decoded = arrow_table_to_array(pa.Table.from_batches([normalized]), array.type)
    assert decoded == array
    assert decoded.content_id() == array.content_id()


def test_decoding_ignores_child_nulls_and_references_under_nullable_parents() -> None:
    schema = optional(struct_of(ticket=IdSchema(), tokens=int))
    array_type = Array.from_items([], ("[case: int]", schema)).type
    expected_schema = array_arrow_schema(array_type)
    # 36 bytes that are not a CID, which decoding would refuse if it read them.
    visible, hidden = cid_for(1842), bytes(36)
    fields = list(expected_schema.field(1).type)
    value = pa.StructArray.from_arrays(
        [pa.array([visible, hidden], type=pa.binary(36)), pa.array([120, None], type=pa.int64())],
        fields=fields,
        mask=pa.array([False, True]),
    )
    table = pa.Table.from_arrays([pa.array([1842, 2091]), value], schema=expected_schema)

    decoded = arrow_table_to_array(table, array_type)
    assert decoded.raw_rows == (
        ((1842,), {"ticket": EntityID(cid=visible), "tokens": 120}),
        ((2091,), None),
    )


def test_decoding_rejects_visible_child_nulls_after_nested_normalization() -> None:
    array = Array.scalar({"metrics": {"quality": 95}}, struct_of(metrics=struct_of(quality=int)))
    batch = pa.RecordBatch.from_arrays(
        [pa.array([{"quality": None}], type=pa.struct([pa.field("quality", pa.int64())]))],
        names=["metrics"],
    )
    normalized = normalize_arrow_batch(batch, array.type)
    with pytest.raises(ArrowArrayError, match="Non-nullable schema int contains a null"):
        arrow_table_to_array(pa.Table.from_batches([normalized]), array.type)


def test_batch_decoding_cancels_before_reading_values() -> None:
    array = Array.from_items([((1842,), {"tokens": 120})], ("[ticket: int]", struct_of(tokens=int)))
    table = pa.Table.from_batches(
        array_to_arrow_batches(array), schema=array_arrow_schema(array.type)
    )

    def cancel() -> None:
        raise RuntimeError("collection cancelled")

    with pytest.raises(RuntimeError, match="collection cancelled"):
        arrow_table_to_array(table, array.type, check_cancelled=cancel)


def test_batch_decoding_preserves_values_across_one_large_arrow_chunk() -> None:
    array = Array.from_items(
        (
            ((index,), {"tokens": index, "metadata": {"labels": ["evaluation", str(index)]}})
            for index in range(8193)
        ),
        ("[row: int]", struct_of(tokens=int, metadata=struct_of(labels=list_of(str)))),
    )
    table = pa.Table.from_batches(
        array_to_arrow_batches(array), schema=array_arrow_schema(array.type)
    )
    assert len(table.to_batches()) == 1
    decoded = arrow_table_to_array(table, array.type)
    assert decoded == array
    assert decoded.content_id() == array.content_id()


def test_batch_decoding_rejects_invalid_empty_struct_placeholders() -> None:
    array = Array.scalar({"events": [{}, {}]}, struct_of(events=list_of(struct_of())))
    table = pa.Table.from_arrays(
        [pa.array([[False, True]], type=array_arrow_schema(array.type).field(0).type)],
        schema=array_arrow_schema(array.type),
    )
    with pytest.raises(ArrowArrayError, match="empty-struct placeholder must be false"):
        arrow_table_to_array(table, array.type)


@pytest.mark.parametrize("invalid", [float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("shape", ["scalar", "struct", "list", "nullable_struct", "nested_list"])
def test_direct_arrow_builders_reject_non_finite_floats_at_every_depth(
    invalid: float,
    shape: str,
) -> None:
    match shape:
        case "scalar":
            array = Array.scalar(invalid, float)
        case "struct":
            array = Array.scalar({"quality": invalid}, struct_of(quality=float))
        case "list":
            array = Array.scalar([0.95, None, invalid], list_of(optional(float)))
        case "nullable_struct":
            array = Array.scalar({"quality": invalid}, optional(struct_of(quality=float)))
        case _:
            array = Array.scalar(
                [{"metrics": None}, {"metrics": {"quality": [invalid]}}],
                list_of(struct_of(metrics=optional(struct_of(quality=list_of(float))))),
            )
    with pytest.raises(ArrowArrayError, match="An array cannot hold NaN or an infinite float"):
        list(array_to_arrow_batches(array))


def test_direct_arrow_builders_allow_null_floats_in_nullable_parents_and_lists() -> None:
    array = Array.from_items(
        [
            ((1842,), {"metrics": None, "scores": [None, 0.95]}),
            ((2091,), {"metrics": {"quality": 0.0}, "scores": []}),
        ],
        (
            "[ticket: int]",
            struct_of(
                metrics=optional(struct_of(quality=float)),
                scores=list_of(optional(float)),
            ),
        ),
    )
    table = pa.Table.from_batches(
        array_to_arrow_batches(array), schema=array_arrow_schema(array.type)
    )
    assert arrow_table_to_array(table, array.type) == array


@pytest.mark.parametrize("only_null_row", [False, True])
def test_list_decoding_ignores_hidden_values_under_null_lists(only_null_row: bool) -> None:
    array_type = Array.from_items(
        [],
        ("[case: int]", optional(list_of(struct_of(ticket=IdSchema(), tokens=int)))),
    ).type
    expected_schema = array_arrow_schema(array_type)
    list_type = expected_schema.field(1).type
    # 36 bytes that are not a CID, which decoding would refuse if it read them.
    visible, hidden = cid_for(1842), bytes(36)
    values = pa.StructArray.from_arrays(
        [pa.array([visible, hidden], type=pa.binary(36)), pa.array([120, None], type=pa.int64())],
        fields=list(list_type.value_type),
    )
    lists = pa.ListArray.from_arrays(
        pa.array([0, 1, 2]),
        values,
        type=list_type,
        mask=pa.array([False, True]),
    )
    table = pa.Table.from_arrays([pa.array([1842, 2091]), lists], schema=expected_schema)
    if only_null_row:
        table = table.slice(1)

    decoded = arrow_table_to_array(table, array_type)
    assert decoded.raw_rows == (
        (((2091,), None),)
        if only_null_row
        else (
            ((1842,), ({"ticket": EntityID(cid=visible), "tokens": 120},)),
            ((2091,), None),
        )
    )
