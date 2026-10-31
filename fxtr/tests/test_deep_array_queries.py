"""Deep value shapes use real Arrow, DuckDB, canonical encoding, and entity storage."""

from __future__ import annotations

import pyarrow as pa
import pytest
from deep_array_testing import SUPPORT_CASES, evidence_ids, leaf_column, nested_array

from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array, ArrayTypeError
from fxtr.entity_defns.arrow import (
    array_arrow_schema,
    array_to_arrow_batches,
    arrow_table_content_id,
    arrow_table_to_array,
    validate_arrow_batches,
)
from fxtr.entity_defns.query import QueryError, count, value
from fxtr.entity_defns.schema import optional, struct_of
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio

SHAPES = [(False, depth) for depth in (0, 1, 4, 16, 32)] + [
    (True, depth) for depth in (1, 4, 8, 16)
]


@pytest.mark.parametrize(("lists", "depth"), SHAPES)
@pytest.mark.parametrize("batch_size", [1, 3])
def test_deep_arrow_roundtrip_preserves_nulls_lists_and_exact_canonical_identity(
    lists: bool, depth: int, batch_size: int
) -> None:
    array = nested_array(depth, lists=lists)
    batches = list(array_to_arrow_batches(array, batch_size=batch_size))
    table = pa.Table.from_batches(batches, schema=array_arrow_schema(array.type))
    validate_arrow_batches(table.to_batches(), array.type)
    assert arrow_table_to_array(table, array.type) == array
    assert arrow_table_content_id(table, array.type) == array.content_id()


@pytest.mark.parametrize("depth", [1, 4, 16, 32])
async def test_deep_struct_leaf_filter_projection_and_cached_requery(depth: int) -> None:
    array = nested_array(depth)
    original_id = array.content_id()
    expected = Array.from_items(
        [((i,), {"quality": i * 7 + 9, "record": array[(i,)]}) for i in (2, 4, 5, 6)],
        (
            "[row: int]",
            struct_of(quality=optional(int), record=array.schema),
        ),
    )
    for _ in range(2):
        query = array.filter(leaf_column(depth) >= 20).select(
            {"quality": leaf_column(depth), "record": value()}
        )
        result = await query.collect()
        assert result == expected
        assert (
            await result.aggregate(
                {"n": count(), "tokens": value().field("record").field("tokens").sum()}
            ).collect()
        ).item() == {"n": 4, "tokens": 337}
    assert array._backing.arrow_conversions == 1
    assert array.content_id() == original_id


@pytest.mark.parametrize("depth", [1, 4, 8, 16])
async def test_deep_nullable_lists_survive_multiplied_join_and_projection(depth: int) -> None:
    array = nested_array(depth, lists=True)
    lookup = Array.from_items(
        [((i,), {"group": i % 2, "tier": f"tier-{i}"}) for i in range(4)],
        ("[lookup: int]", struct_of(group=int, tier=str)),
    )
    query = array.join(lookup, on=[(value().field("group"), value().field("group"))]).select(
        {"record": value().field("left"), "tier": value().field("right").field("tier")}
    )
    expected = Array.from_items(
        [
            ((i, j), {"record": row, "tier": config["tier"]})
            for (i,), row in array.items()
            for (j,), config in lookup.items()
            if row["group"] == config["group"]
        ],
        query.type,
    )
    result = await query.collect()
    assert result == expected
    assert result.content_id() == expected.content_id()
    assert (await result.aggregate({"rows": count()}).collect()).item() == {"rows": 16}


@pytest.mark.parametrize(("lists", "depth"), [(False, 32), (True, 12)])
async def test_deep_entity_ids_remain_loadable_and_opaque(lists: bool, depth: int) -> None:
    storage = MemoryStorage()
    bound = await storage.store(SUPPORT_CASES["refund"])
    array = nested_array(depth, lists=lists, reference=bound.entity_id)
    result = await array.query().select({"record": value()}).collect()
    references = evidence_ids(tuple(result.values()))
    assert len(references) == 12
    for reference in references:
        assert isinstance(reference, BoundID)
        assert reference.entity_id == bound.entity_id
        assert await storage.load(reference) == SUPPORT_CASES["refund"]
    copied = await result.select({"copy": value()}).collect()
    assert len(evidence_ids(tuple(copied.values()))) == 12
    if not lists:
        for field in ("question", "cid"):
            with pytest.raises(ArrayTypeError, match="No value field path"):
                array.select({"broken": leaf_column(depth, "primary").field(field)})


@pytest.mark.parametrize(("lists", "depth"), [(False, 96), (True, 48)])
async def test_native_depth_error_does_not_resolve_result_or_leak_capacity(
    lists: bool, depth: int
) -> None:
    query = nested_array(depth, lists=lists, rows=1).select({"record": value()})
    for _ in range(2):
        with pytest.raises((QueryError, pa.ArrowInvalid), match="[Rr]ecursion"):
            await query.collect()
        assert query._executor.query_limiter.borrowed_tokens == 0
    control = Array.from_items([((917,), 80), ((1042,), 120)], ("[row: int]", int))
    assert (
        await control.aggregate({"rows": count(), "tokens": value().sum()}).collect()
    ).item() == {
        "rows": 2,
        "tokens": 200,
    }


@pytest.mark.parametrize(("lists", "depth"), [(False, 96), (True, 48)])
def test_an_empty_array_round_trips_at_a_depth_whose_rows_arrow_refuses(
    lists: bool, depth: int
) -> None:
    """Arrow's recursion limit is reached by nested values, not by the schema that holds them,
    so these depths round trip with no rows to build."""
    array = nested_array(depth, lists=lists, rows=0)
    table = pa.Table.from_batches(
        array_to_arrow_batches(array), schema=array_arrow_schema(array.type)
    )
    assert table.num_rows == 0
    assert arrow_table_to_array(table, array.type) == array
