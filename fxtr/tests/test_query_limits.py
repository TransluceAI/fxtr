"""Optional query budgets and natural system boundaries, using real runtimes."""

from __future__ import annotations

import dataclasses
import math
import random
import sys
from typing import Any, cast

import anyio
import cbrrr
import duckdb
import pytest
from frozendict import frozendict

from fxtr.duckdb_engine.compiler import compile_array_query
from fxtr.duckdb_engine.executor import DuckDBArrowExecutor, DuckDBExecutionLimits
from fxtr.duckdb_engine.local_query import query_arrays
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.query import LiteralExpression, Not, Predicate, QueryError, dim, value
from fxtr.entity_defns.query_plan import (
    PlannedQuery,
    QueryFilter,
    QueryLimit,
    QueryPlan,
    QuerySelect,
    QuerySource,
    QuerySQL,
    query_type,
)
from fxtr.entity_defns.schema import LeafSchema
from fxtr.runner.query_codec import decode_query, encode_query

pytestmark = pytest.mark.anyio


async def test_default_query_settings_impose_no_additional_caps() -> None:
    limits = DuckDBExecutionLimits()
    assert all(getattr(limits, field.name) is None for field in dataclasses.fields(limits))
    executor = DuckDBArrowExecutor()
    assert executor.query_limiter.total_tokens == math.inf


def test_a_query_node_repeats_a_float_sum_it_could_split_across_threads() -> None:
    """A workflow's query node runs again on every attempt, so it runs on one DuckDB thread: over
    enough rows for DuckDB to split the work, a sum of floats of mixed magnitudes differs in its
    last digits between runs on several threads, and repeats on one."""
    rng = random.Random(0)
    rows = Array.from_items(
        [((i,), rng.uniform(-1, 1) * 10.0 ** rng.randint(-8, 8)) for i in range(600_000)],
        ("[i: int]", float),
    )
    plan = PlannedQuery.over(0, rows.type).aggregate({"total": value().sum()}).plan
    assert len({query_arrays(plan, [rows]).item()["total"] for _ in range(5)}) == 1


@pytest.mark.parametrize("configured", [False, True])
async def test_native_settings_are_inherited_unless_explicitly_configured(configured: bool) -> None:
    native_config: dict[str, str | bool | int | float | list[str]] = (
        {"memory_limit": "64MB", "threads": "1"} if configured else {}
    )
    with duckdb.connect(":memory:", config=native_config) as connection:
        expected_memory, expected_threads = connection.execute(
            "SELECT current_setting('memory_limit'), current_setting('threads')"
        ).fetchone() or (None, None)
    executor = DuckDBArrowExecutor(
        limits=DuckDBExecutionLimits(memory_limit="64MB", threads=1) if configured else None
    )
    # User SQL may not read the host's settings, so this asks a connection the executor opened.
    connection = executor._connect()
    try:
        settings = connection.execute(
            "SELECT current_setting('memory_limit'), current_setting('threads')"
        ).fetchone()
    finally:
        connection.close()
    assert settings == (expected_memory, expected_threads)


@pytest.mark.parametrize("timeout", [None, 1.0])
async def test_operation_timeout_is_not_misreported_as_a_configured_deadline(
    timeout: float | None,
) -> None:
    executor = DuckDBArrowExecutor(limits=DuckDBExecutionLimits(timeout_seconds=timeout))
    with pytest.raises(TimeoutError, match="storage connection timed out"):
        async with executor.query_scope():
            raise TimeoutError("storage connection timed out")
    assert executor.query_limiter.borrowed_tokens == 0


async def test_default_query_capacity_does_not_serialize_six_callers() -> None:
    executor = DuckDBArrowExecutor()
    admitted = anyio.Event()
    release = anyio.Event()
    active = 0

    async def query() -> None:
        nonlocal active
        async with executor.query_scope():
            active += 1
            if active == 6:
                admitted.set()
            await release.wait()

    with anyio.fail_after(1):
        async with anyio.create_task_group() as tasks:
            for _ in range(6):
                tasks.start_soon(query)
            await admitted.wait()
            assert executor.query_limiter.borrowed_tokens == 6
            release.set()
    assert executor.query_limiter.borrowed_tokens == 0


async def test_query_larger_than_one_mib_round_trips_and_executes() -> None:
    annotation = "customer requested a detailed delivery investigation\n" * 25_000
    query = QuerySelect(
        QuerySource(0), frozendict({"annotation": LiteralExpression(annotation, LeafSchema("str"))})
    )
    encoded = encode_query(query)
    assert len(encoded) > 1_048_576
    decoded = decode_query(encoded)
    assert encode_query(decoded) == encoded
    result = query_arrays(decoded, [Array.scalar(917, int)])
    assert result.item() == {"annotation": annotation}


def test_typing_a_plan_or_expression_too_deep_for_python_is_a_query_error() -> None:
    array = Array.from_items([((917,), 80)], ("[ticket: int]", int))
    query: QueryPlan = QuerySource(0)
    for _ in range(sys.getrecursionlimit()):
        query = QueryLimit(query, 1, (), "first")
    with pytest.raises(QueryError, match="recursion limit"):
        query_type(query, [array.type])
    expression: Any = value()
    for _ in range(sys.getrecursionlimit()):
        expression = expression + 1
    with pytest.raises(QueryError, match="recursion limit"):
        array.select(expression)


def test_a_sql_plans_output_type_is_checked_for_names_duckdb_cannot_tell_apart() -> None:
    output_type = ArrayType.of(("[i: int, I: int]", int))
    plan = QuerySQL(
        frozendict({"t": QuerySource(0)}), "SELECT dims, value FROM t", frozendict(), output_type
    )
    with pytest.raises(QueryError, match="differ only by case"):
        query_type(plan, [output_type])


@pytest.mark.parametrize("nesting", ["plan", "predicate"])
async def test_query_deeper_than_128_operations_round_trips_and_executes(nesting: str) -> None:
    query: QueryPlan = QuerySource(0)
    predicate: Predicate = value() >= 0
    for _ in range(160):
        if nesting == "plan":
            query = QueryFilter(query, predicate)
        else:
            predicate = Not(predicate)
    if nesting == "predicate":
        query = QueryFilter(query, predicate)
    encoded = encode_query(query)
    decoded = decode_query(encoded)
    assert encode_query(decoded) == encoded
    array = Array.from_items([((917,), 80), ((1042,), 120)], ("[ticket: int]", int))
    assert query_arrays(decoded, [array]) == array


async def test_encoding_reports_the_actual_python_recursion_limit() -> None:
    query: QueryPlan = QuerySource(0)
    for _ in range(sys.getrecursionlimit() + 1):
        query = QueryLimit(query, 1, (), "first")
    with pytest.raises(QueryError, match="encoding exceeds Python's recursion limit"):
        encode_query(query)


async def test_decoding_reports_the_actual_python_recursion_limit() -> None:
    query: dict[str, object] = {"op": "source", "index": 0}
    for _ in range(sys.getrecursionlimit() + 1):
        query = {"op": "limit", "input": query, "count": 1, "order_by": [], "ties": "first"}
    # cbrrr's iterative encoder lets a real peer send a plan deeper than Python can traverse.
    encoded = cbrrr.encode_dag_cbor(cast("cbrrr.DagCborTypes", query))
    with pytest.raises(QueryError, match="decoding exceeds Python's recursion limit"):
        decode_query(encoded)


async def test_compilation_reports_recursion_before_preparing_or_running_an_array() -> None:
    query: QueryPlan = QuerySource(0)
    for _ in range(sys.getrecursionlimit() + 1):
        query = QueryFilter(query, dim("ticket") >= 0)
    with pytest.raises(QueryError, match="compilation exceeds Python's recursion limit"):
        compile_array_query(query, [ArrayType.of(("[ticket: int]", int))], ["tickets"])


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"memory_limit": ""}, "memory_limit"),
        ({"threads": 0}, "threads"),
        ({"timeout_seconds": 0}, "finite and positive"),
        ({"timeout_seconds": math.inf}, "finite and positive"),
        ({"timeout_seconds": math.nan}, "finite and positive"),
        ({"max_result_rows": -1}, "max_result_rows"),
        ({"max_result_bytes": -1}, "max_result_bytes"),
    ],
)
async def test_invalid_explicit_limits_are_rejected_early(
    limits: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        DuckDBExecutionLimits(**limits)  # type: ignore[reportArgumentType]
