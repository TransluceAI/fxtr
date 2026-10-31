"""User-written DuckDB SQL composes with checked arrays without losing their invariants."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import anyio
import pytest

import fxtr
from fxtr.core import codec
from fxtr.core.entity_data import EntityID
from fxtr.duckdb_engine.executor import (
    DuckDBExecutionLimits,
    DuckDBResourceLimitError,
)
from fxtr.duckdb_engine.local_query import query_arrays
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.query import LiteralExpression, QueryError, count, dim, value
from fxtr.entity_defns.query_plan import PlannedQuery, QuerySource, QuerySQL
from fxtr.entity_defns.schema import IdSchema, list_of, optional, struct_of
from fxtr.entity_defns.sql import SQLParameters, bind_sql, normalize_sql_parameters
from fxtr.runner.query_codec import decode_query, encode_query
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio

ROWS_TYPE = ArrayType.of(("[model: str, run: int]", struct_of(model=str, score=float)))


def rows() -> Array[Any]:
    return Array.from_items(
        [
            (("agent-v3", 101), {"model": "preview", "score": 0.91}),
            (("agent-v3", 102), {"model": "production", "score": 0.97}),
            (("agent-v2", 103), {"model": "production", "score": 0.82}),
            (("agent-v2", 104), {"model": "preview", "score": 0.82}),
        ],
        ROWS_TYPE,
    )


@pytest.mark.parametrize(
    "text,parameters,expected,values",
    [
        ("SELECT ?, ?", [0.8, 3], "SELECT $5, $6", (0.8, 3)),
        ("SELECT $2, $1, $2", [0.8, 3], "SELECT $5, $6, $5", (3, 0.8)),
        ("SELECT ?2, ?", [0.8, 3, 7], "", ()),
        ("SELECT $Minimum, $minimum", {"MINIMUM": 0.8}, "SELECT $5, $5", (0.8,)),
        (
            "SELECT '$minimum ?', $minimum -- $ignored ?\n;",
            {"minimum": 0.8},
            "SELECT '$minimum ?', $5 -- $ignored ?\n",
            (0.8,),
        ),
        (
            'SELECT "$minimum", $$literal $minimum ?$$, $minimum /* $ignored */',
            {"minimum": 0.8},
            'SELECT "$minimum", $$literal $minimum ?$$, $5 /* $ignored */',
            (0.8,),
        ),
        ("SELECT '評価 🧪', $最小値", {"最小値": 0.8}, "SELECT '評価 🧪', $5", (0.8,)),
        # DuckDB folds ASCII letters only, so ß and ss are two names.
        ("SELECT $ß, $ss", {"ß": 1, "ss": 2}, "SELECT $5, $6", (1, 2)),
    ],
)
def test_sql_bindings_are_parser_based(
    text: str, parameters: SQLParameters, expected: str, values: tuple[object, ...]
) -> None:
    if not expected:
        with pytest.raises(QueryError, match="unexpected"):
            bind_sql(text, normalize_sql_parameters(parameters), 4)
    else:
        assert bind_sql(text, normalize_sql_parameters(parameters), 4) == (expected, values)


@pytest.mark.parametrize(
    "parameters,message",
    [
        # DuckDB would convert these to one type rather than bind them as given.
        ({"p": [1, "a"]}, "one type"),
        ({"p": [{"a": 1}, {"b": 2}]}, "one type"),
        ({"p": [1, 2.5]}, "one type"),
        ({"p": {}}, "empty struct"),
    ],
)
def test_parameters_duckdb_would_convert_are_refused(
    parameters: SQLParameters, message: str
) -> None:
    with pytest.raises(QueryError, match=message):
        normalize_sql_parameters(parameters)


async def test_text_that_is_not_valid_unicode_is_refused() -> None:
    with pytest.raises(QueryError, match="not valid Unicode"):
        fxtr.sql("SELECT dims, value FROM t -- \ud800", tables={"t": rows()})
    with pytest.raises(QueryError, match="not valid Unicode"):
        fxtr.sql(
            "SELECT dims, value FROM t",
            tables={"t": rows(), "\ud800": rows()},
            output_type=ROWS_TYPE,
        )


@pytest.mark.parametrize(
    "text",
    [
        "",
        " -- only a comment",
        "SELECT 1; SELECT 2",
        "CREATE TABLE evaluations(score DOUBLE)",
        "DELETE FROM input",
        "COPY input TO '/tmp/fixture-sql-test'",
        "SET enable_external_access=true",
        "INSTALL httpfs",
        "LOAD httpfs",
        "ATTACH '/tmp/fixture-sql-test' AS other",
        "SELECT FROM",
    ],
)
def test_invalid_sql_is_rejected_without_conversion(text: str) -> None:
    array = rows()
    with pytest.raises(QueryError):
        fxtr.sql(text, tables={"input": array})
    assert not array._backing.has_table


@pytest.mark.parametrize(
    "parameters",
    [
        "minimum",
        b"minimum",
        None,
        {"minimum": object()},
        {"minimum": float("inf")},
        {"minimum": float("nan")},
        {"minimum": {7: "invalid"}},
        {7: 0.8},
        {"": 0.8},
        {"MINIMUM": 0.8, "minimum": 0.9},
    ],
)
def test_invalid_sql_parameter_shapes_are_actionable(parameters: object) -> None:
    with pytest.raises(QueryError, match="SQL parameter"):
        fxtr.sql(
            "SELECT dims, value FROM input WHERE value.score >= $minimum",
            tables={"input": rows()},
            parameters=cast("Any", parameters),
        )


def test_parameter_and_table_inputs_are_frozen() -> None:
    array = rows()
    minimum = {"minimum": [0.8, 0.9]}
    tables = {"catalogue": rows()}
    query = fxtr.sql(
        "SELECT dims, value FROM input WHERE list_contains($minimum, value.score)",
        tables={"input": array, **tables},
        parameters=minimum,
        output_type=ROWS_TYPE,
    )
    minimum["minimum"].append(0.7)
    tables.clear()
    assert isinstance(query._query.plan, QuerySQL)
    assert query._query.plan.parameters["minimum"] == (0.8, 0.9)
    assert set(query._query.plan.inputs) == {"input", "catalogue"}


@pytest.mark.parametrize(
    "tables,message",
    [
        ({}, "needs tables"),
        ({"input": rows(), "INPUT": rows()}, "distinct ignoring case"),
        ({"__fxtr_input_0": rows()}, "reserved for fxtr"),
        ({"input": rows(), "catalogue": object()}, "all concrete arrays and local queries"),
        ({"input": "rows"}, "all concrete arrays and local queries"),
    ],
)
def test_sql_tables_reject_ambiguous_or_nonlocal_inputs(tables: object, message: str) -> None:
    with pytest.raises(QueryError, match=message):
        fxtr.sql("SELECT 1", tables=cast("Any", tables), output_type=ROWS_TYPE)


def test_sql_assumes_a_result_type_only_for_a_single_table() -> None:
    assert fxtr.sql("SELECT dims, value FROM runs", tables={"runs": rows()}).type == ROWS_TYPE

    with pytest.raises(QueryError, match="needs output_type when it reads more than one table"):
        fxtr.sql("SELECT dims, value FROM runs", tables={"runs": rows(), "others": rows()})


async def test_sql_names_every_table_and_none_is_called_input_unless_the_caller_says_so() -> None:
    result = await fxtr.sql(
        "SELECT runs.dims, runs.value FROM runs SEMI JOIN best USING (dims)",
        tables={"runs": rows(), "best": rows().filter(value().field("score") > 0.95)},
        output_type=ROWS_TYPE,
    ).collect()

    assert list(result.keys()) == [("agent-v3", 102)]
    with pytest.raises(QueryError, match="reads 'input', which is not one of its tables"):
        fxtr.sql("SELECT dims, value FROM input", tables={"runs": rows()})


async def test_window_query_is_lazy_and_preserves_arrow_for_selectors() -> None:
    array = rows()
    query = fxtr.sql(
        "WITH ranked AS (SELECT *, row_number() OVER (PARTITION BY dims.model "
        "ORDER BY value.score DESC, dims.run) AS rank FROM input) "
        "SELECT dims, value FROM ranked WHERE rank <= $per_model AND value.model=$state",
        tables={"input": array},
        parameters={"per_model": 1, "state": "production"},
    )
    assert not array._backing.has_table
    summary = (await query.aggregate({"rows": count()}).collect()).item()
    assert summary == {"rows": 2}
    assert array._backing.arrow_conversions == 1
    resolved = await query._run()
    assert not resolved._backing.has_rows and resolved._backing.has_table
    collected = await query.collect()
    assert collected == Array.from_items([array.raw_rows[0], array.raw_rows[3]], ROWS_TYPE)
    # Comparing decodes a batch at a time and keeps no rows.
    assert collected._backing.python_materializations == 0


async def test_complex_inequality_left_join_and_nested_projection() -> None:
    array = rows()
    bands = Array.from_items(
        [(("high",), {"minimum": 0.9, "maximum": 1.0})],
        ("[band: str]", struct_of(minimum=float, maximum=float)),
    )
    type = ArrayType.of(
        ("[model: str, run: int]", struct_of(model=str, detail=struct_of(band=str, score=float)))
    )
    query = fxtr.sql(
        "SELECT r.dims AS dims, {model:r.value.model, detail:"
        "{band:coalesce(b.dims.band, 'standard'), score:r.value.score}} AS value "
        "FROM input AS r LEFT JOIN bands AS b "
        "ON r.value.score >= b.value.minimum AND r.value.score < b.value.maximum",
        tables={
            "input": array.filter(value().field("score") >= 0.8),
            "bands": fxtr.sql("SELECT dims,value FROM input", tables={"input": bands}),
        },
        output_type=type,
    )
    actual = await query.collect()
    expected = Array.from_items(
        [
            (
                key,
                {
                    "model": row["model"],
                    "detail": {
                        "band": "high" if row["score"] >= 0.9 else "standard",
                        "score": row["score"],
                    },
                },
            )
            for key, row in array.items()
        ],
        type,
    )
    assert actual == expected
    assert query._executor.metrics.queries == 1


async def test_unnest_results_use_explicit_unique_dimension_keys() -> None:
    array = Array.from_items(
        [((101,), {"samples": [0.9, None, 0.8]}), ((102,), {"samples": []})],
        ("[run: int]", struct_of(samples=list_of(optional(float)))),
    )
    query = fxtr.sql(
        "SELECT {run:dims.run, position:s.position} AS dims, {score:s.score} AS value "
        "FROM input, UNNEST(value.samples) WITH ORDINALITY AS s(score,position)",
        tables={"input": array},
        output_type=("[run: int, position: int]", struct_of(score=optional(float))),
    )
    assert await query.collect() == Array.from_items(
        [
            ((101, 1), {"score": 0.9}),
            ((101, 2), {"score": None}),
            ((101, 3), {"score": 0.8}),
        ],
        query.type,
    )


async def test_sql_grouping_and_dimensionless_aggregates() -> None:
    query = fxtr.sql(
        "SELECT {model:dims.model} AS dims, "
        "{rows:count(*), median:quantile_cont(value.score, 0.5)} AS value "
        "FROM input GROUP BY dims.model",
        tables={"input": rows()},
        output_type=("[model: str]", struct_of(rows=int, median=optional(float))),
    )
    assert await query.collect() == Array.from_items(
        [
            (("agent-v2",), {"rows": 2, "median": 0.82}),
            (("agent-v3",), {"rows": 2, "median": 0.94}),
        ],
        query.type,
    )
    summary = fxtr.sql(
        "SELECT {rows:sum(value.rows)::BIGINT} AS value",
        tables={"input": query},
        output_type=struct_of(rows=int),
    )
    # A query may omit FROM only when it does not reference the input relation.
    with pytest.raises(QueryError, match="Binder Error"):
        (await summary.collect()).item()
    assert (
        await fxtr.sql(
            "SELECT {rows:sum(value.rows)::BIGINT} AS value FROM input",
            tables={"input": query},
            output_type=struct_of(rows=int),
        ).collect()
    ).item() == {"rows": 4}


async def test_empty_scalar_nullable_and_empty_struct_results() -> None:
    assert (
        len(
            await fxtr.sql(
                "SELECT dims,value FROM input WHERE FALSE", tables={"input": rows()}
            ).collect()
        )
        == 0
    )
    assert (
        await fxtr.sql(
            "SELECT NULL::DOUBLE AS value", tables={"input": rows()}, output_type=optional(float)
        ).collect()
    ).item() is None
    assert (
        await fxtr.sql(
            "SELECT FALSE AS value", tables={"input": rows()}, output_type=struct_of()
        ).collect()
    ).item() == {}
    scalar = Array.scalar(0.9, float)
    assert (await fxtr.sql("SELECT * FROM input", tables={"input": scalar}).collect()).item() == 0.9


@pytest.mark.parametrize(
    "sql,type,message",
    [
        ("SELECT value FROM input", None, "SQL result must return"),
        ("SELECT dims,value,1 AS extra FROM input", None, "SQL result must return"),
        ("SELECT {run:dims.run} AS dims,value FROM input", None, "fields do not match"),
        ("SELECT dims,{score:value.score} AS value FROM input", None, "fields do not match"),
        (
            "SELECT dims,{model:value.model,score:value.score,extra:1} AS value FROM input",
            None,
            "fields do not match",
        ),
        (
            "SELECT NULL::STRUCT(model VARCHAR, run BIGINT) AS dims,value FROM input",
            None,
            "dimensions cannot be null",
        ),
        (
            "SELECT dims,NULL::STRUCT(score DOUBLE) AS value FROM input",
            ("[model:str,run:int]", struct_of(score=optional(float))),
            "value cannot be null",
        ),
        (
            "SELECT dims,{score:1.0/0.0} AS value FROM input",
            ("[model:str,run:int]", struct_of(score=float)),
            "finite",
        ),
        (
            "SELECT dims,{score:1.2} AS value FROM input",
            ("[model:str,run:int]", struct_of(score=int)),
            "but output_type declares",
        ),
        # A value of another kind is refused, not converted, even where a conversion exists.
        (
            "SELECT dims,{score:7} AS value FROM input",
            ("[model:str,run:int]", struct_of(score=bool)),
            "but output_type declares",
        ),
        (
            "SELECT dims,{score:TIMESTAMP '2020-01-01 00:00:01'} AS value FROM input",
            ("[model:str,run:int]", struct_of(score=int)),
            "but output_type declares",
        ),
        (
            "SELECT dims,{score:'5'} AS value FROM input",
            ("[model:str,run:int]", struct_of(score=int)),
            "but output_type declares",
        ),
        (
            "SELECT {model:dims.run,run:dims.run} AS dims,value FROM input",
            None,
            "but output_type declares",
        ),
        (
            "SELECT dims,value FROM input UNION ALL SELECT dims,value FROM input",
            None,
            "[Dd]uplicate|strictly increasing",
        ),
        (
            "SELECT value.score AS value FROM input",
            float,
            "no dimensions must have exactly one row",
        ),
    ],
)
async def test_invalid_outputs_do_not_resolve_and_next_query_succeeds(
    sql: str, type: object, message: str
) -> None:
    root = rows().query()
    query = fxtr.sql(sql, tables={"input": root}, output_type=cast("Any", type))
    with pytest.raises(QueryError, match=message):
        await query.collect()
    assert query._executor.query_limiter.borrowed_tokens == 0
    assert (
        await fxtr.sql("SELECT dims,value FROM input", tables={"input": root}).collect() == rows()
    )


@pytest.mark.parametrize(
    ("sql", "declared", "expression", "expected"),
    [
        ("SELECT 5 AS value", float, value() // 2, 2.5),
        ("SELECT {'x': 5} AS value", struct_of(x=float), value().field("x") // 2, 2.5),
        ("SELECT 2.5 AS value", float, value() * 2, 5.0),
    ],
)
async def test_an_operation_on_a_sql_result_reads_its_declared_type(
    sql: str, declared: object, expression: Any, expected: float
) -> None:
    result = fxtr.sql(sql, tables={"input": rows()}, output_type=cast("Any", declared))
    assert (await result.select(expression).collect()).item() == expected
    assert (await (await result.collect()).select(expression).collect()).item() == expected


async def test_sql_reads_real_cids() -> None:
    storage = MemoryStorage()
    reference = await storage.store_raw_data({"$type": "tests.SupportTicket", "ticket": "TKT-1842"})
    array = Array.from_items(
        [((1842,), reference)], ("[ticket:int]", IdSchema("tests.SupportTicket"))
    )
    root = array.query()
    query = fxtr.sql(
        "SELECT dims,value FROM input WHERE value=$ticket",
        tables={"input": root},
        parameters={"ticket": reference},
    )
    collected = await query.collect()
    returned = next(collected.raw_values())
    assert isinstance(returned, EntityID) and returned == reference
    contents = await storage.load_raw_data(returned)
    assert isinstance(contents, Mapping) and contents["ticket"] == "TKT-1842"
    exported = await fxtr.sql(
        "SELECT dims,value AS value FROM input",
        tables={"input": root},
        output_type=("[ticket:int]", bytes),
    ).collect()
    assert next(exported.raw_values()) == reference.cid
    # The same bytes bound as a parameter are an ID of the same entity.
    bound = await fxtr.sql(
        "SELECT dims,$cid AS value FROM input",
        tables={"input": root},
        parameters={"cid": reference.cid},
    ).collect()
    restored = next(bound.raw_values())
    assert isinstance(restored, EntityID) and restored == reference
    # Bytes that are not a CID in an ID column fail the structural check on the result.
    with pytest.raises(QueryError, match="DAG-CBOR SHA-256 CID"):
        await fxtr.sql(
            "SELECT dims,unhex('FF' || substr(hex(value),3)) AS value FROM input",
            tables={"input": root},
        ).collect()
    assert await fxtr.sql("SELECT dims,value FROM input", tables={"input": root}).collect() == array


async def test_sql_entity_membership_relabeling_and_parameters() -> None:
    storage = MemoryStorage()
    present = await storage.store_raw_data({"$type": "tests.SupportTicket", "ticket": "TKT-1842"})
    outsider = await storage.store_raw_data({"$type": "tests.SupportTicket", "ticket": "TKT-2091"})
    array = Array.from_items(
        [((1842,), present)], ("[ticket:int]", IdSchema("tests.SupportTicket"))
    )
    root = array.query()
    assert (
        len(
            await fxtr.sql(
                "SELECT dims,value FROM input WHERE value=$ticket",
                tables={"input": root},
                parameters={"ticket": outsider},
            ).collect()
        )
        == 0
    )
    # SQL may write an ID its inputs did not hold, as Python may build one.
    written = await fxtr.sql(
        "SELECT dims,$ticket AS value FROM input",
        tables={"input": root},
        parameters={"ticket": outsider},
    ).collect()
    assert list(written.raw_values()) == [outsider]
    # A reference may be declared as another entity type, as EntityID.bind_type allows in Python.
    relabeled = await fxtr.sql(
        "SELECT dims,value FROM input",
        tables={"input": root},
        output_type=("[ticket:int]", IdSchema("tests.OtherEntity")),
    ).collect()
    assert list(relabeled.raw_values()) == [present]
    assert (
        await fxtr.sql(
            "SELECT $ordinary AS value",
            tables={"input": array},
            parameters={"ordinary": present.cid},
            output_type=bytes,
        ).collect()
    ).item() == present.cid


async def test_nested_nullable_ids_round_trip_through_sql() -> None:
    storage = MemoryStorage()
    reference = await storage.store_raw_data({"$type": "tests.SupportTicket", "ticket": "TKT-1842"})
    schema = struct_of(
        detail=optional(struct_of(ticket=IdSchema(), history=list_of(optional(IdSchema())))),
        bytes=bytes,
    )
    array = Array.from_items(
        [
            ((100,), {"detail": None, "bytes": reference.cid}),
            (
                (101,),
                {
                    "detail": {"ticket": reference, "history": [None, reference]},
                    "bytes": reference.cid,
                },
            ),
            ((102,), {"detail": None, "bytes": reference.cid}),
        ],
        ("[run:int]", schema),
    )
    query = fxtr.sql("SELECT dims,value FROM input", tables={"input": array.query()})
    assert await query.collect() == array


async def test_an_entity_join_over_nested_sql_runs_as_one_statement() -> None:
    storage = MemoryStorage()
    reference = await storage.store_raw_data({"$type": "tests.SupportTicket", "ticket": "TKT-1842"})
    type = ArrayType.of(("[run:int]", IdSchema("tests.SupportTicket")))
    left = Array.from_items([((101,), reference)], type)
    right = Array.from_items([((102,), reference)], type)
    query = fxtr.sql(
        'SELECT dims,value FROM input WHERE value."right"=$ticket',
        tables={
            "input": fxtr.sql(
                'SELECT {run:l.dims.run} AS dims, {"left":l.value,"right":r.value} AS value '
                "FROM input AS l JOIN catalogue AS r ON l.value=r.value WHERE r.value=$ticket",
                tables={
                    "input": left.query().filter(dim("run") >= 101),
                    "catalogue": fxtr.sql(
                        "SELECT dims,value FROM input",
                        tables={"input": right.query()},
                    ),
                },
                parameters={"ticket": reference},
                output_type=("[run:int]", struct_of(left=type.schema, right=type.schema)),
            ).filter(value().field("left") == LiteralExpression(reference, type.schema))
        },
        parameters={"ticket": reference},
    )
    result = await query.collect()
    assert query._executor.metrics.queries == 1
    assert result[(101,)]["left"] == result[(101,)]["right"] == reference
    assert await storage.load_raw_data(result[(101,)]["right"]) == await storage.load_raw_data(
        reference
    )


@pytest.mark.parametrize(
    "sql,schema,message",
    [
        (
            "SELECT {detail:{score:NULL::DOUBLE}} AS value",
            struct_of(detail=struct_of(score=float)),
            "contains a null",
        ),
        (
            "SELECT {history:[NULL::DOUBLE]} AS value",
            struct_of(history=list_of(float)),
            "contains a null",
        ),
        (
            "SELECT {detail:{score:'NaN'::DOUBLE}} AS value",
            struct_of(detail=optional(struct_of(score=float))),
            "finite",
        ),
        (
            "SELECT {history:[1.0/0.0]} AS value",
            struct_of(history=list_of(optional(float))),
            "finite",
        ),
        ("SELECT TRUE AS value", struct_of(), "must be false"),
    ],
)
async def test_generated_nested_values_obey_canonical_schema(
    sql: str, schema: object, message: str
) -> None:
    with pytest.raises(QueryError, match=message):
        (
            await fxtr.sql(sql, tables={"input": rows()}, output_type=cast("Any", schema)).collect()
        ).item()


async def test_sql_struct_list_null_and_unicode_parameters_execute_without_interpolation() -> None:
    array = Array.from_items(
        [((101,), {"a.b": 0.91, "評価": "production"})],
        ("[run:int]", struct_of(**{"a.b": float, "評価": str})),
    )
    assert (
        await fxtr.sql(
            'SELECT dims,value FROM input WHERE value."a.b" >= $filter.minimum '
            'AND list_contains($states,value."評価") AND $unused IS NULL',
            tables={"input": array},
            parameters={"filter": {"minimum": 0.9}, "states": ["production"], "unused": None},
        ).collect()
        == array
    )
    assert (
        await fxtr.sql(
            "SELECT NULL::STRUCT(score DOUBLE) AS value",
            tables={"input": array},
            output_type=optional(struct_of(score=float)),
        ).collect()
    ).item() is None


async def test_concurrent_sql_queries_keep_parameters_connections_and_results_separate() -> None:
    array = rows()
    root = array.query()
    results: dict[int, Array[Any]] = {}

    async def query(run: int) -> None:
        results[run] = await fxtr.sql(
            "SELECT dims,value FROM input WHERE dims.run=?",
            tables={"input": root},
            parameters=[run],
        ).collect()

    async with anyio.create_task_group() as tasks:
        for run in range(101, 109):
            tasks.start_soon(query, run)
    for run, result in results.items():
        assert result == Array.from_items(
            [row for row in array.raw_rows if row[0][1] == run], ROWS_TYPE
        )
    assert array._backing.arrow_conversions == 1
    assert root._executor.query_limiter.borrowed_tokens == 0


HIDDEN_TYPE = ArrayType.of(("[run: int]", struct_of(score=int, note=str)))


def partly_hidden() -> Array[Any]:
    return Array.from_items(
        [((1,), {"score": 10, "note": "kept"}), ((2,), {"score": 99, "note": "filtered out"})],
        HIDDEN_TYPE,
    )


FLAT_PROJECTION = "SELECT {run: run} AS dims, {score: score, note: note} AS value FROM "


@pytest.mark.parametrize(
    "text,message",
    [
        (FLAT_PROJECTION + "__fxtr_input_0", "not one of its tables"),
        (FLAT_PROJECTION + '"__FXTR_INPUT_0"', "not one of its tables"),
        ("SELECT dims, value FROM sqlite_master", "not one of its tables"),
        ("SELECT dims, value FROM duckdb_views", "not one of its tables"),
        ("SELECT dims, value FROM input WHERE EXISTS (FROM other)", "not one of its tables"),
        # A WITH name is only a WITH query where SQL scoping makes it visible.
        (
            "SELECT * FROM duckdb_views "
            "UNION ALL (WITH duckdb_views AS (SELECT 1) SELECT * FROM duckdb_views)",
            "not one of its tables",
        ),
        (
            "WITH duckdb_views AS (SELECT * FROM duckdb_views) SELECT * FROM duckdb_views",
            "not one of its tables",
        ),
        (
            "WITH first AS (SELECT * FROM later), later AS (SELECT 1) SELECT * FROM first",
            "not one of its tables",
        ),
        # The first side of a recursive WITH is an ordinary query: its own name is not in scope.
        (
            "WITH RECURSIVE duckdb_views AS (SELECT view_name FROM duckdb_views UNION ALL "
            "SELECT view_name FROM duckdb_views WHERE false) SELECT * FROM duckdb_views",
            "not one of its tables",
        ),
        # DuckDB folds ASCII letters only: a long s and a Kelvin sign are other names to it.
        (
            'WITH "duckdb_view\u017f" AS (SELECT 1 AS x) SELECT * FROM duckdb_views',
            "not one of its tables",
        ),
        (
            'WITH "duc\u212adb_views" AS (SELECT 1 AS x) SELECT * FROM duckdb_views',
            "not one of its tables",
        ),
        (
            "SELECT dims, value FROM input WHERE EXISTS (WITH hidden AS (SELECT 1) FROM hidden) "
            "AND EXISTS (FROM hidden)",
            "not one of its tables",
        ),
        ("SELECT dims, value FROM main.input", "schema or catalog"),
        ("SELECT dims, value FROM input, information_schema.tables", "schema or catalog"),
        ("SELECT * FROM query_table('__fxtr_' || 'input_0')", r"table function query_table\(\)"),
        ("SELECT * FROM query('SELECT 1')", r"table function query\(\)"),
        (
            "SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT 1'))",
            r"table function json_execute_serialized_sql\(\)",
        ),
        ("SELECT * FROM duckdb_views()", r"table function duckdb_views\(\)"),
        ("SELECT * FROM read_csv('/etc/passwd')", r"table function read_csv\(\)"),
        ("SELECT * FROM (SHOW TABLES)", "cannot use SHOW"),
        (
            "SELECT {run: 1} AS dims, {score: 1, note: current_query()} AS value",
            r"cannot call current_query\(\)",
        ),
        (
            "SELECT {run: 1} AS dims, {score: 1, note: current_setting('memory_limit')} AS value",
            r"cannot call current_setting\(\)",
        ),
        (
            "SELECT dims, value FROM input WHERE setseed(0.5) IS NULL",
            r"cannot call setseed\(\).*outlives the query",
        ),
        ("SELECT dims, value FROM input WHERE sleep_ms(1) IS NULL", r"cannot call sleep_ms\(\)"),
        ("SELECT dims, value FROM input WHERE pg_sleep(1) IS NULL", r"cannot call pg_sleep\(\)"),
        ('WITH "__fxtr_mine" AS (SELECT 1) SELECT dims, value FROM input', "reserved for fxtr"),
    ],
)
async def test_sql_cannot_read_anything_but_its_named_tables(text: str, message: str) -> None:
    visible = partly_hidden().filter(value().field("score") < 50)

    with pytest.raises(QueryError, match=message) as failure:
        await fxtr.sql(text, tables={"input": visible}, output_type=HIDDEN_TYPE).collect()

    assert "filtered out" not in str(failure.value)


@pytest.mark.parametrize(
    "text",
    [
        "WITH kept AS (SELECT * FROM input) SELECT dims, value FROM kept",
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) "
        "SELECT dims, value FROM input WHERE dims.run IN (SELECT i FROM n)",
        "WITH kept AS (SELECT * FROM input), again AS (SELECT * FROM kept) "
        "SELECT dims, value FROM again WHERE EXISTS (FROM kept)",
        "SELECT dims, value FROM input, range(1) AS once(i)",
        "SELECT dims, value FROM input, generate_series(1, 1) AS once(i)",
        "SELECT dims, value FROM input, unnest([1]) AS once(i)",
        "SELECT dims, value FROM INPUT /* __fxtr_input_0 */ WHERE value.note <> '__fxtr_input_0'",
    ],
)
async def test_sql_reads_named_tables_with_queries_and_row_generators(text: str) -> None:
    visible = partly_hidden().filter(value().field("score") < 50)

    result = await fxtr.sql(text, tables={"input": visible}, output_type=HIDDEN_TYPE).collect()

    assert result.rows == (((1,), {"score": 10, "note": "kept"}),)


async def test_sql_reads_a_value_field_whose_name_starts_like_a_private_relation() -> None:
    type = ArrayType.of(("[run: int]", struct_of(__fxtr_input_0=int)))
    array = Array.from_items([((1,), {"__fxtr_input_0": 7})], type)

    result = await fxtr.sql(
        "SELECT dims, value FROM input WHERE value.__fxtr_input_0 = 7",
        tables={"input": array},
        output_type=type,
    ).collect()

    assert result == array


def test_a_wire_plan_whose_sql_names_a_private_relation_is_rejected_at_decode() -> None:
    data = {
        "op": "sql",
        "inputs": {"input": {"op": "source", "index": 0}},
        "text": "SELECT * FROM __fxtr_input_0",
        "parameters": {},
        "output_type": ROWS_TYPE.to_wire(),
    }

    with pytest.raises(QueryError, match="not one of its tables"):
        decode_query(codec.encode(cast("Any", data)))


@pytest.mark.parametrize(
    "patch",
    [
        {"text": "DELETE FROM input"},
        {"text": 42},
        {"inputs": {}},
        {"inputs": {"other": {"op": "source", "index": 0}}},
        {"inputs": {"input": {"op": "source", "index": True}}},
        {"parameters": []},
        {"parameters": {"unused": 1}},
        {"output_type": {"dims": [], "schema": "invalid"}},
        {"unexpected": 1},
    ],
)
def test_malformed_sql_wire_descriptions_are_rejected(patch: dict[str, Any]) -> None:
    data = {
        "op": "sql",
        "inputs": {"input": {"op": "source", "index": 0}},
        "text": "SELECT dims,value FROM input",
        "parameters": {},
        "output_type": ROWS_TYPE.to_wire(),
    }
    with pytest.raises(QueryError):
        decode_query(codec.encode(cast("Any", data | patch)))


async def test_a_sql_plan_runs_as_its_encoding_decodes() -> None:
    query = PlannedQuery.sql(
        "SELECT dims,value FROM input WHERE value.score >= $minimum",
        tables={"input": PlannedQuery.over("rows", ROWS_TYPE)},
        parameters={"minimum": 0.9},
        output_type=ROWS_TYPE,
    ).plan
    encoded = encode_query(query)
    assert encode_query(decode_query(encoded)) == encoded
    result = query_arrays(decode_query(encoded), [rows()])
    assert list(result.keys()) == [("agent-v3", 101), ("agent-v3", 102)]


async def test_a_sql_plan_cannot_address_a_source_that_was_not_supplied() -> None:
    query = PlannedQuery.sql(
        "SELECT dims,value FROM input",
        tables={"input": PlannedQuery(("missing", "rows"), QuerySource(1), ROWS_TYPE)},
        output_type=ROWS_TYPE,
    ).plan
    query = decode_query(encode_query(query))
    source = rows()
    with pytest.raises(QueryError, match="outside the 1 supplied inputs"):
        query_arrays(query, [source])
    # The plan is refused while it is compiled, before an input is converted to Arrow.
    assert source._backing.arrow_conversions == 0


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM read_csv('https://example.invalid/data.csv')",
    ],
)
async def test_sql_cannot_read_files_or_network_and_connection_remains_usable(sql: str) -> None:
    root = rows().query()
    with pytest.raises(QueryError, match="cannot call the table function read_"):
        await fxtr.sql(sql, tables={"input": root}, output_type=str).collect()
    assert (
        await fxtr.sql("SELECT dims,value FROM input", tables={"input": root}).collect() == rows()
    )


async def test_sql_deadlines_and_result_limits_use_existing_executor_controls() -> None:
    capped = fxtr.sql(
        "SELECT dims,value FROM input",
        tables={"input": rows()},
        limits=DuckDBExecutionLimits(max_result_rows=1),
    )
    with pytest.raises(DuckDBResourceLimitError, match="exceeded 1 rows"):
        await capped.collect()
    slow = fxtr.sql(
        "SELECT sum(i)::BIGINT AS value FROM range(10000000000) AS r(i)",
        tables={"input": rows()},
        output_type=int,
        limits=DuckDBExecutionLimits(timeout_seconds=0.05),
    )
    with pytest.raises(DuckDBResourceLimitError, match="0.05 seconds"):
        (await slow.collect()).item()
    assert slow._executor.query_limiter.borrowed_tokens == 0


async def test_a_dims_struct_with_a_declared_type_that_has_no_dims_is_a_mismatch() -> None:
    total = "SELECT {run: 1} AS dims, sum(value.score) AS value FROM t"

    with pytest.raises(QueryError, match="output_type has no dims"):
        await fxtr.sql(total, tables={"t": rows()}, output_type=float).collect()
    passed_through = fxtr.sql(
        "SELECT dims, value FROM t", tables={"t": Array.scalar(0.5, float)}, output_type=float
    )
    assert (await passed_through.collect()).item() == 0.5


@pytest.mark.parametrize(
    ("parameter", "message"),
    [
        (2**63, "SQL parameter 'p' int must fit in 64 bits"),
        ({"inner": [-(2**63) - 1]}, "SQL parameter 'p' int must fit in 64 bits"),
        ("\ud800", "SQL parameter 'p' string must be valid Unicode text"),
    ],
)
def test_a_parameter_an_array_could_not_hold_is_refused_when_the_query_is_built(
    parameter: object, message: str
) -> None:
    with pytest.raises(QueryError, match=message):
        fxtr.sql(
            "SELECT dims, value FROM t WHERE $p IS NOT NULL",
            tables={"t": rows()},
            parameters={"p": cast("Any", parameter)},
        )


def test_writing_to_the_hosts_duckdb_log_is_refused() -> None:
    with pytest.raises(QueryError, match=r"cannot call write_log\(\)"):
        fxtr.sql("SELECT dims, value FROM t WHERE write_log('x') IS NULL", tables={"t": rows()})


def test_a_long_statement_is_parsed_but_not_kept_in_the_parse_cache() -> None:
    from fxtr.entity_defns import sql as sql_module

    # Long without being deep: DuckDB refuses an expression nested past 1,000 levels.
    options = ", ".join(str(number) for number in range(20_000))
    long_text = f"SELECT dims, value FROM t WHERE dims.run NOT IN ({options})"
    assert len(long_text) > sql_module._MAX_CACHED_SQL_CHARACTERS
    kept_before = sql_module._parse_sql_cached.cache_info().currsize

    sql_module.validate_sql_reads(long_text, ["t"])
    with pytest.raises(QueryError, match="reads 'other'"):
        sql_module.validate_sql_reads(long_text.replace("FROM t", "FROM other"), ["t"])

    assert sql_module._parse_sql_cached.cache_info().currsize == kept_before
