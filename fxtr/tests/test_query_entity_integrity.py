"""Entity references remain opaque and loadable across real DuckDB query boundaries."""

from __future__ import annotations

import pyarrow as pa
import pytest
from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entity_data import EntityID, UnknownReferenceError
from fxtr.duckdb_engine.local_query import query_arrays
from fxtr.entity_defns.array import Array, ArrayType, ArrayTypeError
from fxtr.entity_defns.arrow import (
    ArrowArrayError,
    array_arrow_schema,
    array_to_arrow_batches,
    arrow_table_content_id,
    arrow_table_entity_ids,
    arrow_table_to_array,
)
from fxtr.entity_defns.query import (
    Column,
    LiteralExpression,
    QueryError,
    count,
    dim,
    value,
    when,
)
from fxtr.entity_defns.query_plan import QuerySelect, QuerySource
from fxtr.entity_defns.schema import (
    FrozenValue,
    IdSchema,
    Schema,
    list_of,
    optional,
    schema_to_wire,
    struct_of,
)
from fxtr.runner.postgres.database import Database
from fxtr.runner.postgres.entities import PostgresEntityStorage
from fxtr.runner.query_codec import decode_query

pytestmark = pytest.mark.anyio


def _dangling() -> EntityID:
    return EntityID(
        cid=codec.compute_cid(codec.encode({"$type": "tests.SupportTicket", "ticket": "TKT-2091"}))
    )


@pytest.mark.parametrize("shape", ["scalar", "nullable", "list", "struct"])
def test_literal_schema_cannot_promote_bytes_into_an_entity_id(shape: str) -> None:
    cid = _dangling().cid
    schema: Schema
    literal: FrozenValue
    match shape:
        case "nullable":
            schema, literal = optional(IdSchema()), cid
        case "list":
            schema, literal = list_of(IdSchema()), (cid,)
        case "struct":
            schema, literal = struct_of(evidence=list_of(IdSchema())), frozendict(evidence=(cid,))
        case _:
            schema, literal = IdSchema(), cid
    with pytest.raises(QueryError, match="expected an entity id"):
        LiteralExpression(literal, schema)
    encoded = codec.encode(
        {
            "op": "select",
            "input": {"op": "source", "index": 0},
            "fields": {
                "ticket": {"kind": "literal", "value": literal, "schema": schema_to_wire(schema)}
            },
        }
    )
    with pytest.raises(QueryError, match="expected an entity id"):
        decode_query(encoded)


def test_literal_without_a_schema_must_be_null() -> None:
    with pytest.raises(QueryError, match="must be null"):
        LiteralExpression(_dangling().cid, None)
    assert LiteralExpression(None, None).value is None


async def test_queries_need_not_return_the_ids_their_inputs_hold() -> None:
    array = Array.from_items(
        [((1842,), {"ticket": _dangling(), "tokens": 120})],
        ("[ticket: int]", struct_of(ticket=IdSchema(), tokens=int)),
    )
    assert await array.select({"tokens": value().field("tokens")}).collect() == Array.from_items(
        [((1842,), {"tokens": 120})], ("[ticket: int]", struct_of(tokens=int))
    )
    assert (await array.aggregate({"tickets": count()}).collect()).item() == {"tickets": 1}
    assert len(await array.filter(dim("ticket") == 2091).collect()) == 0


async def test_entity_id_counts_remain_opaque() -> None:
    first = _dangling()
    second = EntityID(
        cid=codec.compute_cid(codec.encode({"$type": "tests.SupportTicket", "ticket": "TKT-2092"}))
    )
    array = Array.from_items(
        [((1,), first), ((2,), first), ((3,), second), ((4,), None)],
        ("[row: int]", optional(IdSchema())),
    )

    assert (
        await array.aggregate(
            {
                "rows": count(),
                "references": value().count(),
                "unique_references": value().n_unique(),
            }
        ).collect()
    ).item() == {"rows": 4, "references": 3, "unique_references": 3}


async def test_computed_expressions_keep_entity_ids_opaque() -> None:
    reference = _dangling()
    array = Array.from_items([((1,), reference)], ("[row: int]", IdSchema()))

    # Equality and membership can produce ordinary Boolean values without returning the ID.
    compared = await array.select(
        {
            "equal": value() == LiteralExpression(reference, IdSchema()),
            "member": value().is_in([LiteralExpression(reference, IdSchema())]),
        }
    ).collect()
    assert compared[(1,)] == {"equal": True, "member": True}

    with pytest.raises(ArrayTypeError, match=r"cast\(\) requires a leaf value"):
        array.select(value().cast(str))
    with pytest.raises(ArrayTypeError, match="Arithmetic left operand must be an int or float"):
        array.select(value() + 1)
    with pytest.raises(ArrayTypeError, match=r"between\(\) expression must be an int or float"):
        array.filter(value().between(0, 1))

    # An ID constant may fill a missing ID or be a branch's value, as a `$` parameter may in SQL.
    nullable = Array.from_items([((1,), None)], ("[row: int]", optional(IdSchema())))
    filled = await nullable.select(
        value().fill_null(LiteralExpression(reference, IdSchema()))
    ).collect()
    assert filled[(1,)] == reference
    branched = await nullable.select(
        when(dim("row") == 2).then(value()).otherwise(LiteralExpression(reference, IdSchema()))
    ).collect()
    assert branched[(1,)] == reference


@pytest.mark.parametrize("chained", [False, True])
def test_entity_contents_and_id_bytes_cannot_be_addressed_as_fields(chained: bool) -> None:
    array = Array.scalar({"ticket": _dangling()}, struct_of(ticket=IdSchema("tests.SupportTicket")))
    for field in ("subject", "cid"):
        expression = Column("value", ("ticket", field))
        if chained:
            expression = value().field("ticket").field(field)
        with pytest.raises(ArrayTypeError, match="No value field path"):
            array.select({"extracted": expression})


@pytest.mark.parametrize("operation", ["encode", "decode", "input"])
def test_arrow_rejects_36_byte_values_that_are_not_fixture_cids(operation: str) -> None:
    array_type = ArrayType.of(IdSchema())
    table = pa.Table.from_arrays(
        [pa.array([b"x" * 36], type=pa.binary(36))], schema=array_arrow_schema(array_type)
    )
    with pytest.raises(ArrowArrayError, match="DAG-CBOR SHA-256 CID"):
        if operation == "encode":
            arrow_table_content_id(table, array_type)
        elif operation == "decode":
            arrow_table_to_array(table, array_type)
        else:
            tuple(array_to_arrow_batches(Array.scalar(EntityID(cid=b"x" * 36), IdSchema())))


def test_arrow_entity_ids_ignore_hidden_nullable_children_and_sliced_list_elements() -> None:
    visible, hidden = _dangling().cid, b"x" * 36
    schema = list_of(optional(struct_of(ticket=IdSchema())))
    array_type = ArrayType.of(("[ticket: int]", schema))
    child = pa.StructArray.from_arrays(
        [pa.array([hidden, visible, hidden, hidden], type=pa.binary(36))],
        names=["ticket"],
        mask=pa.array([False, False, True, False]),
    )
    lists = pa.ListArray.from_arrays(pa.array([0, 1, 3, 4], type=pa.int32()), child).slice(1, 1)
    table = pa.Table.from_arrays([pa.array([1842]), lists], schema=array_arrow_schema(array_type))
    assert arrow_table_entity_ids(table, array_type) == (EntityID(cid=visible),)


async def test_postgres_rejects_an_input_reference_it_does_not_know(database: Database) -> None:
    """An input's rows are taken as given, so a reference Postgres cannot vouch for reaches the
    result. Storing it is what refuses the query, and the reference stays unknown."""
    seed = PostgresEntityStorage(database)
    reference = await seed.store_raw_data({"$type": "tests.SupportTicket", "ticket": "TKT-1842"})
    rows = Array.from_items([((1842,), reference)], ("[ticket: int]", IdSchema()))
    source = await seed.store(rows)
    await seed.flush()
    dangling = Array.from_items([((1842,), _dangling())], rows.type)
    reader = PostgresEntityStorage(database)
    result = query_arrays(QuerySelect(QuerySource(0), frozendict(ticket=value())), [dangling])
    with pytest.raises(UnknownReferenceError, match="referencing unknown entity"):
        await reader.store(result)
    assert not reader.is_known(_dangling())
    assert not reader._pending
    assert await reader.load(source) == rows
