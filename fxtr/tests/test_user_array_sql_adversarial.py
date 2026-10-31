"""Adversarial SQL shapes preserve fxtr array and execution invariants."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from fxtr import sql as array_sql
from fxtr.duckdb_engine.executor import DuckDBExecutionLimits, DuckDBResourceLimitError
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.query import QueryError, count, value
from fxtr.entity_defns.schema import list_of, optional, struct_of

pytestmark = pytest.mark.anyio


def scored_rows() -> Array[Any]:
    return Array.from_items(
        [
            ((1,), {"group": "a", "score": 0.4}),
            ((2,), {"group": "a", "score": 0.9}),
            ((3,), {"group": "b", "score": 0.7}),
            ((4,), {"group": "b", "score": 0.8}),
        ],
        ("[run:int]", struct_of(group=str, score=float)),
    )


async def test_quoted_and_injection_shaped_identifiers_remain_data() -> None:
    type = (
        "[select:int]",
        struct_of(**{"a.b": str, 'say"what': int, "評価": str}),
    )
    source = Array.from_items([((7,), {"a.b": "safe", 'say"what': 3, "評価": "良い"})], type)
    table_name = 'input"), stolen AS (SELECT * FROM secret); -- 評価'
    quoted_table = '"' + table_name.replace('"', '""') + '"'
    result = await array_sql(
        f"SELECT source.dims, source.value FROM {quoted_table} AS source",
        tables={table_name: source},
        output_type=type,
    ).collect()
    assert result == source


async def test_trailing_semicolon_comments_and_placeholder_like_text_execute() -> None:
    source = scored_rows()
    result = await array_sql(
        "SELECT dims,value FROM input "
        "WHERE value.score >= $minimum AND '$ignored ?' = '$ignored ?'; "
        "-- $ignored __fxtr_input_0 query_table(\n",
        tables={"input": source},
        parameters={"minimum": 0.8},
        output_type=source.type,
    ).collect()
    assert list(result.items()) == list(source.items())[1::2]


async def test_parameter_values_cannot_change_query_structure() -> None:
    source = scored_rows()
    injected = "a' OR TRUE; SELECT dims,value FROM input --"
    result = await array_sql(
        'SELECT dims,value FROM input WHERE value."group"=$group',
        tables={"input": source},
        parameters={"group": injected},
        output_type=source.type,
    ).collect()
    assert len(result) == 0


@pytest.mark.parametrize(
    "text",
    [
        'SELECT DISTINCT ON (value."group") dims,value FROM input '
        'ORDER BY value."group",value.score DESC',
        "SELECT dims,value FROM input QUALIFY row_number() OVER "
        '(PARTITION BY value."group" ORDER BY value.score DESC)=1',
        "SELECT dims,value FROM input WHERE EXISTS ("
        "SELECT 1 FROM input AS other WHERE other.dims.run=input.dims.run "
        "AND other.value.score>=0.8)",
        "SELECT dims,value FROM input WHERE value.score>=0.8 "
        "INTERSECT SELECT dims,value FROM input",
    ],
)
async def test_distinct_qualify_correlated_and_set_queries_agree(text: str) -> None:
    source = scored_rows()
    result = await array_sql(text, tables={"input": source}, output_type=source.type).collect()
    assert list(result.items()) == list(source.items())[1::2]


async def test_sql_order_is_discarded_when_array_keys_are_canonicalized() -> None:
    source = scored_rows()
    result = await array_sql(
        "SELECT dims,value FROM input ORDER BY dims.run DESC",
        tables={"input": source},
        output_type=source.type,
    ).collect()
    assert result == source


async def test_dimension_and_value_field_with_same_name_stay_distinct() -> None:
    source = Array.from_items(
        [(("dimension-model",), {"model": "value-model"})],
        ("[model:str]", struct_of(model=str)),
    )
    assert (
        await array_sql(
            "SELECT dims,value FROM input "
            "WHERE dims.model='dimension-model' AND value.model='value-model'",
            tables={"input": source},
            output_type=source.type,
        ).collect()
        == source
    )


async def test_private_relation_prefix_is_allowed_in_value_field_names() -> None:
    source = Array.from_items(
        [((1,), {"__fxtr_rank": 7}), ((2,), {"__fxtr_rank": 9})],
        ("[run:int]", struct_of(__fxtr_rank=int)),
    )
    result = await array_sql(
        "SELECT dims,value FROM input WHERE value.__fxtr_rank > 7",
        tables={"input": source},
        output_type=source.type,
    ).collect()
    assert result == Array.from_items([source.raw_rows[1]], source.type)


async def test_dimensionless_input_exposes_false_dims_and_scalar_value() -> None:
    source = Array.scalar({"attempts": 3}, struct_of(attempts=int))
    output_type = struct_of(attempts=int, next_attempt=int)
    result = (
        await array_sql(
            "SELECT {attempts:value.attempts,next_attempt:value.attempts+1} AS value "
            "FROM input WHERE dims=FALSE",
            tables={"input": source},
            output_type=output_type,
        ).collect()
    ).item()
    assert result == {"attempts": 3, "next_attempt": 4}


async def test_full_semi_and_anti_joins_have_explicit_valid_dimensions() -> None:
    models = Array.from_items(
        [((1,), {"name": "one"}), ((2,), {"name": "two"}), ((4,), {"name": "four"})],
        ("[id:int]", struct_of(name=str)),
    )
    scores = Array.from_items(
        [((2,), {"score": 20}), ((3,), {"score": 30})],
        ("[id:int]", struct_of(score=int)),
    )
    full_type = (
        "[id:int]",
        struct_of(name=optional(str), score=optional(int)),
    )
    full = await array_sql(
        "SELECT {id:coalesce(m.dims.id,s.dims.id)} AS dims,"
        "{name:m.value.name,score:s.value.score} AS value "
        "FROM models AS m FULL OUTER JOIN scores AS s ON m.dims.id=s.dims.id",
        tables={"models": models, "scores": scores},
        output_type=full_type,
    ).collect()
    assert full == Array.from_items(
        [
            ((1,), {"name": "one", "score": None}),
            ((2,), {"name": "two", "score": 20}),
            ((3,), {"name": None, "score": 30}),
            ((4,), {"name": "four", "score": None}),
        ],
        full_type,
    )
    semi = await array_sql(
        "SELECT m.dims,m.value FROM models AS m SEMI JOIN scores AS s ON m.dims.id=s.dims.id",
        tables={"models": models, "scores": scores},
        output_type=models.type,
    ).collect()
    anti = await array_sql(
        "SELECT m.dims,m.value FROM models AS m ANTI JOIN scores AS s ON m.dims.id=s.dims.id",
        tables={"models": models, "scores": scores},
        output_type=models.type,
    ).collect()
    assert list(semi.items()) == [((2,), {"name": "two"})]
    assert list(anti.items()) == [
        ((1,), {"name": "one"}),
        ((4,), {"name": "four"}),
    ]


async def test_asof_join_keeps_left_keys_and_nullable_right_values() -> None:
    trades = Array.from_items(
        [
            (("A", 5), {"price": 50.0}),
            (("A", 15), {"price": 51.0}),
            (("A", 25), {"price": 52.0}),
        ],
        ("[symbol:str, timestamp:int]", struct_of(price=float)),
    )
    quotes = Array.from_items(
        [(("A", 10), {"ask": 50.5}), (("A", 20), {"ask": 51.5})],
        ("[symbol:str, timestamp:int]", struct_of(ask=float)),
    )
    output_type = (
        "[symbol:str, timestamp:int]",
        struct_of(price=float, ask=optional(float)),
    )
    result = await array_sql(
        "SELECT t.dims,{price:t.value.price,ask:q.value.ask} AS value "
        "FROM trades AS t ASOF LEFT JOIN quotes AS q "
        "ON t.dims.symbol=q.dims.symbol AND t.dims.timestamp>=q.dims.timestamp",
        tables={"trades": trades, "quotes": quotes},
        output_type=output_type,
    ).collect()
    assert result == Array.from_items(
        [
            (("A", 5), {"price": 50.0, "ask": None}),
            (("A", 15), {"price": 51.0, "ask": 50.5}),
            (("A", 25), {"price": 52.0, "ask": 51.5}),
        ],
        output_type,
    )


async def test_lateral_unnest_and_qualify_can_choose_rows_per_input() -> None:
    runs = Array.from_items(
        [
            ((1,), {"events": [{"score": 0.3}, {"score": 0.9}, {"score": 0.8}]}),
            ((2,), {"events": [{"score": 0.6}]}),
        ],
        ("[run:int]", struct_of(events=list_of(struct_of(score=float)))),
    )
    output_type = ("[run:int, position:int]", struct_of(score=float))
    result = await array_sql(
        "SELECT {run:r.dims.run,position:e.position} AS dims,{score:e.event.score} AS value "
        "FROM runs AS r CROSS JOIN LATERAL ("
        "SELECT event,position FROM UNNEST(r.value.events) "
        "WITH ORDINALITY AS u(event,position) "
        "QUALIFY row_number() OVER (ORDER BY event.score DESC,position)<=2"
        ") AS e",
        tables={"runs": runs},
        output_type=output_type,
    ).collect()
    assert result == Array.from_items(
        [
            ((1, 2), {"score": 0.9}),
            ((1, 3), {"score": 0.8}),
            ((2, 1), {"score": 0.6}),
        ],
        output_type,
    )


async def test_recursive_cte_and_set_operations_return_canonical_keys() -> None:
    authorization = Array.scalar(True, bool)
    output_type = ("[number:int]", struct_of(square=int))
    generated = await array_sql(
        "WITH RECURSIVE sequence(number) AS ("
        "SELECT 1 UNION ALL SELECT number+1 FROM sequence WHERE number<100"
        ") SELECT {number:number} AS dims,{square:number*number} AS value FROM sequence "
        "EXCEPT SELECT {number:50} AS dims,{square:2500} AS value",
        tables={"authorization": authorization},
        output_type=output_type,
    ).collect()
    assert len(generated) == 99
    assert generated[(1,)] == {"square": 1}
    assert generated[(100,)] == {"square": 10_000}
    assert (50,) not in generated


async def test_explicit_pivot_and_unpivot_preserve_valid_array_keys() -> None:
    source = scored_rows()
    pivot_type = ("[run:int]", struct_of(a=optional(float), b=optional(float)))
    pivoted = await array_sql(
        'WITH flat AS (SELECT dims.run AS run,value."group" AS cohort,'
        "value.score AS score FROM input), pivoted AS ("
        "PIVOT flat ON cohort IN ('a','b') USING max(score) GROUP BY run) "
        "SELECT {run:run} AS dims,{a:a,b:b} AS value FROM pivoted",
        tables={"input": source},
        output_type=pivot_type,
    ).collect()
    assert pivoted == Array.from_items(
        [
            ((1,), {"a": 0.4, "b": None}),
            ((2,), {"a": 0.9, "b": None}),
            ((3,), {"a": None, "b": 0.7}),
            ((4,), {"a": None, "b": 0.8}),
        ],
        pivot_type,
    )

    with pytest.raises(QueryError, match="read-only SELECT"):
        array_sql(
            'WITH flat AS (SELECT dims.run AS run,value."group" AS cohort,'
            "value.score AS score FROM input) "
            "PIVOT flat ON cohort USING max(score) GROUP BY run",
            tables={"input": source},
            output_type=pivot_type,
        )

    wide = Array.from_items(
        [((1,), {"latency": 0.4, "quality": 0.9}), ((2,), {"latency": 0.7, "quality": None})],
        ("[run:int]", struct_of(latency=float, quality=optional(float))),
    )
    unpivot_type = ("[run:int, metric:str]", float)
    unpivoted = await array_sql(
        "WITH flat AS (SELECT dims.run AS run,value.latency AS latency,"
        "value.quality AS quality FROM input), long AS ("
        "UNPIVOT flat ON latency,quality INTO NAME metric VALUE score) "
        "SELECT {run:run,metric:metric} AS dims,score AS value FROM long",
        tables={"input": wide},
        output_type=unpivot_type,
    ).collect()
    assert unpivoted == Array.from_items(
        [
            ((1, "latency"), 0.4),
            ((1, "quality"), 0.9),
            ((2, "latency"), 0.7),
        ],
        unpivot_type,
    )


async def test_nested_sql_parameters_compile_once_without_materializing_intermediates() -> None:
    source = scored_rows()
    scaled_type = ("[run:int]", struct_of(group=str, score=float))
    scaled = array_sql(
        'SELECT dims,{"group":value."group",score:value.score*$factor} AS value FROM input',
        tables={"input": source},
        parameters={"factor": 10.0},
        output_type=scaled_type,
    )
    selected = array_sql(
        "SELECT dims,value FROM scaled WHERE value.score >= $minimum",
        tables={"scaled": scaled},
        parameters={"minimum": 8.0},
        output_type=scaled_type,
    )
    summary = selected.aggregate({"rows": count(), "total": value().field("score").sum()})
    assert (await summary.collect()).item() == {"rows": 2, "total": 17.0}
    assert summary._executor.metrics.queries == 1
    assert source._backing.arrow_conversions == 1


async def test_self_join_aliases_share_one_arrow_conversion() -> None:
    source = scored_rows()
    result = await array_sql(
        'SELECT l.dims,{"group":l.value."group",score:r.value.score} AS value '
        "FROM left_rows AS l JOIN right_rows AS r ON l.dims.run=r.dims.run",
        tables={"left_rows": source, "right_rows": source},
        output_type=source.type,
    ).collect()
    assert result == source
    assert source._backing.arrow_conversions == 1


async def test_many_named_inputs_compile_and_collect_as_one_query() -> None:
    tables = {f"input {index:02}": Array.scalar(index, int) for index in range(32)}
    statements = [f"SELECT {{source:'{name}'}} AS dims,value FROM \"{name}\"" for name in tables]
    output_type = ("[source:str]", int)
    query = array_sql(" UNION ALL ".join(statements), tables=tables, output_type=output_type)
    result = await query.collect()
    assert result == Array.from_items(
        [((name,), index) for index, name in enumerate(tables)], output_type
    )
    assert query._executor.metrics.queries == 1
    assert all(table._backing.arrow_conversions == 1 for table in tables.values())


async def test_duplicate_keys_spanning_arrow_batches_are_rejected() -> None:
    source = Array.from_items([((index,), index) for index in range(512)], ("[row:int]", int))
    query = array_sql(
        "SELECT dims,value FROM input UNION ALL SELECT dims,value FROM input WHERE dims.row=127",
        tables={"input": source},
        output_type=source.type,
    )
    query._executor.batch_size = 128
    with pytest.raises(QueryError, match="duplicate"):
        await query.collect()


async def test_a_slow_query_is_interrupted_by_the_native_deadline() -> None:
    query = array_sql(
        "SELECT CAST(SUM(i) AS BIGINT) AS value FROM input, range(1000000000000) AS r(i)",
        tables={"input": Array.scalar(1, int)},
        output_type=int,
        limits=DuckDBExecutionLimits(timeout_seconds=0.05),
    )
    with pytest.raises(DuckDBResourceLimitError, match="exceeded 0.05 seconds"):
        (await query.collect()).item()
    assert query._executor.query_limiter.borrowed_tokens == 0


@pytest.mark.parametrize(
    "text,output_type",
    [
        ("SELECT 9223372036854775808::HUGEINT AS value", int),
        ("SELECT 'NaN'::DOUBLE AS value", float),
        ("SELECT [1,NULL,3] AS value", list_of(int)),
        (
            "SELECT {nested:{required:NULL::BIGINT}} AS value",
            struct_of(nested=struct_of(required=int)),
        ),
    ],
)
async def test_noncanonical_generated_values_are_rejected(text: str, output_type: object) -> None:
    query = array_sql(text, tables={"input": scored_rows()}, output_type=output_type)
    with pytest.raises(QueryError):
        await query.collect()


async def test_concurrent_collectors_of_one_query_each_run_it_over_one_converted_input() -> None:
    source = scored_rows()
    query = array_sql(
        "SELECT dims,value FROM input WHERE value.score >= 0.7",
        tables={"input": source},
        output_type=source.type,
    )
    results: list[Array[Any]] = []

    async def collect() -> None:
        results.append(await query.collect())

    async with anyio.create_task_group() as tasks:
        for _ in range(20):
            tasks.start_soon(collect)
    assert len(results) == 20
    assert all(result == results[0] for result in results)
    # A query keeps no result: each collector runs the SQL and gets its own array. What they
    # share is the input's Arrow conversion, which happens once.
    assert len({id(result) for result in results}) == 20
    assert query._executor.metrics.queries == 20
    assert source._backing.arrow_conversions == 1
