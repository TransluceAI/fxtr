"""Reusable local DuckDB connections keep isolation, cleanup, and interruption semantics."""

from __future__ import annotations

import copy
import pickle
from typing import Any

import anyio
import duckdb
import pytest
from anyio.to_thread import run_sync

from fxtr.duckdb_engine.executor import (
    DuckDBArrowExecutor,
    DuckDBExecutionLimits,
    DuckDBResourceLimitError,
    _DuckDBRun,
)
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.arrow import array_arrow_schema, array_to_arrow_batches
from fxtr.entity_defns.query import count, value

pytestmark = pytest.mark.anyio


async def test_sequential_typed_queries_use_isolated_clean_connections() -> None:
    array = Array.from_items([((1842,), 80), ((2091,), 120)], ("[ticket: int]", int))
    root = array.query()
    for _ in range(3):
        assert (await root.aggregate({"tokens": value().sum()}).collect()).item() == {"tokens": 200}
    assert root._executor.metrics.connections_opened == 1
    assert root._executor.metrics.connections_reused == 2
    connection = root._executor._idle_connection
    assert connection is not None
    # The source table was registered as a view; a connection kept for the next query must not
    # still hold it.
    left_behind = "SELECT view_name FROM duckdb_views() WHERE NOT internal"
    assert await run_sync(lambda: connection.sql(left_behind).fetchall()) == []
    with pytest.raises(duckdb.Error, match="locked"):
        await run_sync(lambda: connection.execute("SET enable_external_access = true"))
    root._executor.close_idle_connection()
    assert root._executor._idle_connection is None
    assert (await root.aggregate({"n": count()}).collect()).item() == {"n": 2}


async def test_result_limit_failure_discards_connection_and_keeps_executor_usable() -> None:
    array = Array.from_items([((1842,), 80), ((2091,), 120)], ("[ticket: int]", int))
    root = array.query(limits=DuckDBExecutionLimits(max_result_rows=1))
    assert (await root.aggregate({"n": count()}).collect()).item() == {"n": 2}
    with pytest.raises(DuckDBResourceLimitError, match="exceeded 1 rows"):
        await root.select({"tokens": value()}).collect()
    assert root._executor._idle_connection is None
    assert (await root.aggregate({"n": count()}).collect()).item() == {"n": 2}
    assert root._executor.metrics.connections_opened == 2
    assert root._executor.query_limiter.borrowed_tokens == 0


async def test_completed_run_cannot_interrupt_connection_reused_by_another_run() -> None:
    import pyarrow as pa

    array = Array.from_items([((1842,), 80)], ("[ticket: int]", int))
    table = pa.Table.from_batches(
        array_to_arrow_batches(array), schema=array_arrow_schema(array.type)
    )
    executor = DuckDBArrowExecutor()
    first = _DuckDBRun(executor, "SELECT * FROM source", {"source": table}, array.type, ())
    result = await executor._run_duckdb(first)
    second = _DuckDBRun(
        executor,
        "SELECT * FROM source WHERE (SELECT sum(i) FROM range(100000000) AS t(i)) >= 0",
        {"source": table},
        array.type,
        (),
    )
    completed = anyio.Event()

    async def execute_second() -> None:
        assert (await executor._run_duckdb(second)).equals(result)
        completed.set()

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(execute_second)
            while True:
                with second._lock:
                    running = second._connection is not None
                if running:
                    break
                assert not completed.is_set(), "The native control query ended before interruption"
                await anyio.sleep(0.001)
            first.cancel()
            await completed.wait()
    assert executor.metrics.connections_opened == 1
    assert executor.metrics.connections_reused == 1
    executor.close_idle_connection()


async def test_concurrent_queries_never_share_native_connection() -> None:
    left = Array.from_items([((i,), 7) for i in range(6000)], ("[case: int]", int))
    right = Array.from_items([((i,), 7) for i in range(6000)], ("[replica: int]", int))
    root = left.query(limits=DuckDBExecutionLimits(threads=1))
    await root.limit(1).collect()
    await right.limit(1).collect()
    results: list[Any] = []

    async def collect() -> None:
        results.append(
            (
                await root.join(right, on=[(value(), value())]).aggregate({"n": count()}).collect()
            ).item()
        )

    async with anyio.create_task_group() as tasks:
        for _ in range(4):
            tasks.start_soon(collect)
    assert results == [{"n": 36_000_000}] * 4
    assert root._executor.metrics.connections_opened >= 2
    assert root._executor.query_limiter.borrowed_tokens == 0
    root._executor.close_idle_connection()


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
async def test_interrupted_connection_is_discarded_before_next_query(interrupt: str) -> None:
    left = Array.from_items([((i,), 7) for i in range(50_000)], ("[case: int]", int))
    right = Array.from_items([((i,), 7) for i in range(50_000)], ("[replica: int]", int))
    await left.limit(1).collect()
    await right.limit(1).collect()
    root = left.query(
        limits=DuckDBExecutionLimits(
            threads=1, timeout_seconds=0.2 if interrupt == "timeout" else None
        )
    )
    await root.limit(1).collect()
    query = root.join(right, on=[(value(), value())]).aggregate(
        {"total": value().field("left").sum()}
    )
    if interrupt == "timeout":
        with pytest.raises(DuckDBResourceLimitError, match="exceeded 0.2 seconds"):
            (await query.collect()).item()
    else:
        with anyio.move_on_after(0.1) as scope:
            (await query.collect()).item()
        assert scope.cancel_called
    assert root._executor._idle_connection is None
    assert root._executor.query_limiter.borrowed_tokens == 0
    assert (await root.aggregate({"n": count()}).collect()).item() == {"n": 50_000}
    assert root._executor.metrics.connections_opened == 2
    root._executor.close_idle_connection()


async def test_queries_share_one_idle_connection_unless_they_set_their_own_limits() -> None:
    array = Array.from_items([((1842,), 80), ((2091,), 120)], ("[ticket: int]", int))
    other = Array.from_items([((7,), 1)], ("[ticket: int]", int))
    kept = [array.filter(value() > 0), array.filter(value() > 100), other.filter(value() > 0)]
    for query in kept:
        await query.collect()

    # Every array a program keeps alive would otherwise hold a connection of its own.
    shared = kept[0]._executor
    assert all(query._executor is shared for query in kept)
    assert array.query()._executor is shared
    assert shared.metrics.connections_opened == 1
    limited = array.query(limits=DuckDBExecutionLimits(max_result_rows=10))
    assert limited._executor is not shared
    assert array.query(query_limiter=anyio.CapacityLimiter(1))._executor is not shared


async def test_an_array_that_has_been_queried_still_copies_and_pickles() -> None:
    array = Array.from_items([((1842,), 80), ((2091,), 120)], ("[ticket: int]", int))
    assert (await array.filter(value() > 100).aggregate({"n": count()}).collect()).item() == {
        "n": 1
    }

    for copied in (copy.deepcopy(array), pickle.loads(pickle.dumps(array))):
        assert copied == array
        assert (await copied.filter(value() > 100).aggregate({"n": count()}).collect()).item() == {
            "n": 1
        }
