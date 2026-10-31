"""Failure and boundary cases using real Arrow, DuckDB, and entity storage."""

from __future__ import annotations

import time
from typing import cast

import anyio
import pytest
from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entity_data import EntityID
from fxtr.duckdb_engine.executor import (
    DuckDBExecutionLimits,
    DuckDBResourceLimitError,
)
from fxtr.duckdb_engine.local_query import query_arrays
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.query import LiteralExpression, dim, value
from fxtr.entity_defns.query_plan import (
    QueryAggregate,
    QueryFilter,
    QuerySelect,
    QuerySource,
)
from fxtr.entity_defns.schema import IdSchema, struct_of
from fxtr.runner.query_codec import (
    decode_query,
    encode_query,
)
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("shape", ["scalar", "empty", "dimensioned"])
@pytest.mark.parametrize("operation", ["select", "aggregate", "grouped_aggregate"])
async def test_empty_struct_queries_match_concrete_arrays(shape: str, operation: str) -> None:
    if shape == "scalar":
        rows = Array.scalar(917, int)
    else:
        rows = Array.from_items(
            [] if shape == "empty" else [((17,), 917), ((23,), 1042)],
            ("[ticket: int]", int),
        )
    if operation == "select":
        query = QuerySelect(QuerySource(0), frozendict())
        expected = Array.from_items(
            [(key, {}) for key, _ in rows.raw_rows], (rows.dims, struct_of())
        )
    else:
        groups = (("ticket_value", value()),) if operation == "grouped_aggregate" else ()
        query = QueryAggregate(QuerySource(0), frozendict(), groups)
        expected = (
            Array.from_items(
                [((cast("int", row_value),), {}) for _, row_value in rows.raw_rows],
                ("[ticket_value: int]", struct_of()),
            )
            if groups
            else Array.scalar({}, struct_of())
        )
    result = query_arrays(decode_query(encode_query(query)), [rows])
    assert result == expected
    # Empty structs must remain usable as the input to another query.
    assert query_arrays(QuerySource(0), [result]) == expected


async def test_id_comparison_literals_are_compared_by_content() -> None:
    storage = MemoryStorage()
    reference = await storage.store_raw_data({"$type": "tests.SupportTicket", "ticket": "TKT-917"})
    rows = Array.from_items([((17,), reference)], ("[ticket: int]", IdSchema()))
    query = decode_query(
        encode_query(
            QueryFilter(QuerySource(0), value() == LiteralExpression(reference, IdSchema()))
        )
    )
    assert query_arrays(query, [rows]) == rows


@pytest.mark.parametrize("nested", [False, True])
async def test_id_projection_literals_survive_the_wire_codec(nested: bool) -> None:
    reference = EntityID(
        cid=codec.compute_cid(codec.encode({"$type": "tests.SupportTicket", "ticket": "TKT-917"}))
    )
    literal = LiteralExpression(
        frozendict({"ticket": reference}) if nested else reference,
        struct_of(ticket=IdSchema()) if nested else IdSchema(),
    )
    rows = Array.from_items([((17,), 917)], ("[ticket: int]", int))
    query = decode_query(
        encode_query(QuerySelect(QuerySource(0), frozendict({"unissued_ticket": literal})))
    )
    result = query_arrays(query, [rows])
    assert result.raw_rows == (((17,), {"unissued_ticket": literal.value}),)


async def test_deadline_covers_converting_the_inputs_to_arrow() -> None:
    tickets = Array.from_items(
        (((index,), f"TKT-{index:07d}") for index in range(100_000)), ("[ticket: int]", str)
    )
    limited = tickets.query(limits=DuckDBExecutionLimits(timeout_seconds=0.01))
    started = time.monotonic()
    with pytest.raises(DuckDBResourceLimitError, match="exceeded 0.01 seconds"):
        await limited.limit(1).collect()
    assert time.monotonic() - started < 2, "deadline must not wait for full conversion"


async def test_result_bytes_are_bounded() -> None:
    rows = Array.from_items(
        [((index,), {"customer_has_escalated_ticket": False}) for index in range(1500)],
        ("[ticket: int]", struct_of(customer_has_escalated_ticket=bool)),
    )
    limited = rows.query(limits=DuckDBExecutionLimits(max_result_bytes=2_000))
    with pytest.raises(DuckDBResourceLimitError, match="exceeded 2000 Arrow bytes"):
        await limited.filter(dim("ticket") >= 0).collect()


async def test_queued_query_deadline_does_not_leak_shared_capacity() -> None:
    limiter = anyio.CapacityLimiter(1)
    blocked = Array.scalar(917, int).query(
        query_limiter=limiter, limits=DuckDBExecutionLimits(timeout_seconds=0.01)
    )
    occupied = object()
    await limiter.acquire_on_behalf_of(occupied)
    try:
        with pytest.raises(
            DuckDBResourceLimitError, match="waited 0.01 seconds for query capacity"
        ):
            await blocked.filter(value() > 0).collect()
        assert limiter.borrowed_tokens == 1
        assert limiter.statistics().tasks_waiting == 0
    finally:
        limiter.release_on_behalf_of(occupied)
    recovered = Array.scalar(917, int).query(query_limiter=limiter)
    assert (await recovered.filter(value() > 0).collect()).item() == 917
    assert limiter.borrowed_tokens == 0


async def test_nullable_predicate_negation_remains_null_until_filtering() -> None:
    rows = Array.from_items(
        [((1842,), None), ((2091,), 0.9), ((3017,), 0.7)], ("[ticket: int]", float | None)
    )
    predicate = ~(value() >= 0.8)
    query = decode_query(encode_query(QueryFilter(QuerySource(0), predicate)))
    result = query_arrays(query, [rows])
    assert result == Array.from_items([((3017,), 0.7)], rows.type)
