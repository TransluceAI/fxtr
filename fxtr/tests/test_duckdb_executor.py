"""What the DuckDB executor guarantees about a query's result, through the local queries that run
on it."""

from __future__ import annotations

import time
from typing import Any

import pytest

import fxtr
from fxtr.duckdb_engine import executor
from fxtr.duckdb_engine.executor import DuckDBExecutionLimits, DuckDBResourceLimitError
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.query import QueryError, value
from fxtr.entity_defns.schema import struct_of

pytestmark = pytest.mark.anyio


def _scores() -> Array[object]:
    return Array.from_records(
        [
            {"i": 1, "group": "a", "score": 1},
            {"i": 2, "group": "a", "score": 4},
            {"i": 3, "group": "b", "score": 3},
            {"i": 4, "group": "b", "score": 2},
        ],
        ("[i: int]", struct_of(group=str, score=int)),
    )


async def test_outer_key_order_does_not_change_inner_limit_semantics() -> None:
    result = await fxtr.sql(
        "SELECT dims, value FROM source ORDER BY value.score DESC LIMIT 2",
        tables={"source": _scores()},
    ).collect()

    assert result.rows == (
        ((2,), {"group": "a", "score": 4}),
        ((3,), {"group": "b", "score": 3}),
    )


@pytest.mark.parametrize(
    ("sql", "error", "message"),
    [
        (
            "SELECT dims, value FROM source UNION ALL SELECT dims, value FROM source",
            QueryError,
            "duplicate dimension keys",
        ),
        (
            "SELECT {i: NULL::BIGINT} AS dims, value FROM source LIMIT 1",
            QueryError,
            "null",
        ),
        ("SELECT dims, {score: value.score} AS value FROM source", QueryError, "group"),
    ],
)
async def test_an_invalid_duckdb_result_fails_the_query(
    sql: str, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        await fxtr.sql(sql, tables={"source": _scores()}).collect()


async def test_scalar_result_requires_exactly_one_row() -> None:
    total = fxtr.sql(
        "SELECT SUM(value.score)::BIGINT AS value FROM source",
        tables={"source": _scores()},
        output_type=int,
    )
    assert (await total.collect()).item() == 10

    with pytest.raises(QueryError, match="exactly one row"):
        await fxtr.sql(
            "SELECT value.score AS value FROM source", tables={"source": _scores()}, output_type=int
        ).collect()


async def test_duckdb_connection_blocks_external_file_access() -> None:
    with pytest.raises(QueryError):
        await fxtr.sql(
            "SELECT 1::BIGINT AS value FROM read_csv_auto('/etc/passwd')",
            tables={"source": _scores()},
            output_type=ArrayType.of(int),
        ).collect()


async def test_duckdb_result_row_limit_stops_the_query() -> None:
    limited = _scores().query(limits=DuckDBExecutionLimits(max_result_rows=1))

    with pytest.raises(DuckDBResourceLimitError, match="exceeded 1 rows"):
        await limited.filter(value().field("score") > 0).collect()
    # A query that reads its array unchanged returns that array, but not past the cap.
    with pytest.raises(DuckDBResourceLimitError, match="exceeded 1 rows"):
        await limited.collect()


async def test_duckdb_timeout_interrupts_execution() -> None:
    limited = _scores().query(limits=DuckDBExecutionLimits(timeout_seconds=0.001))

    # A millisecond can run out before the query starts, which is the same limit reported as
    # time spent waiting to start.
    with pytest.raises(DuckDBResourceLimitError, match="0.001 seconds"):
        await fxtr.sql(
            "SELECT CAST(SUM(i) AS BIGINT) AS value FROM source, range(1000000000000) AS r(i)",
            tables={"source": limited},
            output_type=int,
        ).collect()


async def test_a_timeout_between_two_statements_of_a_query_still_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A query runs as several DuckDB statements, and DuckDB drops an interrupt that arrives
    while none of them is executing; the timeout has to stop the next one anyway."""
    read_columns = executor._result_columns

    def slowly(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.2)  # the timeout expires here, before the main statement starts
        return read_columns(*args, **kwargs)

    monkeypatch.setattr(executor, "_result_columns", slowly)
    one_row = Array.scalar(1, int).query(limits=DuckDBExecutionLimits(timeout_seconds=0.05))
    started = time.perf_counter()
    with pytest.raises(DuckDBResourceLimitError, match="0.05 seconds"):
        await fxtr.sql(
            "SELECT CAST(SUM(i) AS BIGINT) AS value FROM source, range(10000000000) AS r(i)",
            tables={"source": one_row},
            output_type=int,
        ).collect()
    assert time.perf_counter() - started < 2  # the main statement alone takes several seconds
