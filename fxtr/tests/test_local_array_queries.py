"""Local query behavior with real Arrow, DuckDB, and fixture entity representations."""

from __future__ import annotations

import dataclasses
import gc
import weakref
from collections.abc import Mapping
from typing import Any, TypedDict, cast

import anyio
import pyarrow as pa
import pytest
from anyio.to_thread import run_sync
from deep_array_testing import leaf_column, nested_array
from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID
from fxtr.duckdb_engine.executor import DuckDBExecutionLimits, DuckDBResourceLimitError
from fxtr.duckdb_engine.local_query import ArrayQuery
from fxtr.entity_defns.array import Array, ArrayTypeError
from fxtr.entity_defns.arrow import ArrowArrayError
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.query import (
    JoinKey,
    LiteralExpression,
    QueryError,
    count,
    desc,
    dim,
    value,
)
from fxtr.entity_defns.query_plan import QueryAggregate, QueryFilter
from fxtr.entity_defns.schema import IdSchema, ListSchema, list_of, optional, struct_of
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio


class Evaluation(TypedDict):
    model: str
    score: float | None
    tokens: int


def evaluations() -> Array[Evaluation]:
    return cast(
        "Array[Evaluation]",
        Array.from_records(
            [
                {"case": "refund", "model": "agent-v3", "score": 0.95, "tokens": 120},
                {"case": "booking", "model": "agent-v3", "score": None, "tokens": 80},
                {"case": "delivery", "model": "agent-v2", "score": 0.7, "tokens": 100},
            ],
            ("[case: str]", Evaluation),
        ),
    )


def test_direct_query_can_be_built_before_starting_event_loop() -> None:
    array = evaluations()
    query = array.filter(value().field("score") >= 0.8)
    assert isinstance(query, ArrayQuery)
    assert not array._backing.has_table
    result = anyio.run(query.collect)
    assert result.rows == ((("refund",), array[("refund",)]),)
    assert array._backing.arrow_conversions == 1


@pytest.mark.parametrize("direct_entry", [False, True])
async def test_local_queries_are_lazy_and_cache_is_not_entity_content(direct_entry: bool) -> None:
    array = evaluations()
    serialized = array._fxtr_serialize()
    query = (
        (array if direct_entry else array.query())
        .filter(value().field("score") >= 0.8)
        .select({"tokens": value().field("tokens")})
    )
    assert isinstance(query, ArrayQuery)
    assert query._executor.metrics.queries == 0
    assert not array._backing.has_table
    assert array._backing.arrow_conversions == 0
    result = await query.collect()
    assert result.rows == ((("refund",), {"tokens": 120}),)
    assert query._executor.metrics.queries == 1
    assert array._backing.arrow_conversions == 1
    assert array._fxtr_serialize() == serialized
    storage = MemoryStorage()
    loaded = await storage.load(await storage.store(array))
    assert loaded == array
    # A load decodes the stored bytes into a table of its own; the query's table stayed behind.
    assert loaded._backing is not array._backing
    assert loaded._arrow_table() is not array._arrow_table()


@pytest.mark.parametrize("explicit_root", [False, True])
@pytest.mark.parametrize("threshold", [0.8, 1.0])
async def test_filter_is_lazy_null_aware_and_reuses_arrow(
    explicit_root: bool, threshold: float
) -> None:
    array = evaluations()
    root = array.query() if explicit_root else array
    predicate = value().field("score") >= threshold
    query = root.filter(predicate)
    repeated = root.filter(predicate)
    assert query._query == repeated._query
    assert query._executor.metrics.queries == 0
    assert repeated._executor.metrics.queries == 0
    assert not array._backing.has_table
    result = await query.collect()
    assert result == await repeated.collect()
    assert list(result.keys()) == ([("refund",)] if threshold == 0.8 else [])
    assert result.type == array.type
    if threshold == 0.8:
        # The filter kept the input's row unchanged, so the result's rows are its row objects.
        assert dict(result.raw_rows)[("refund",)] is dict(array.raw_rows)[("refund",)]
    assert array._backing.arrow_conversions == 1
    assert len(array) == 3


async def test_filter_validates_before_arrow_conversion_and_uses_projected_value() -> None:
    array = evaluations()
    query = array.select({"quality": value().field("score")})
    filtered = query.filter(value().field("quality") >= 0.8)
    with pytest.raises(ArrayTypeError, match="No value field path 'score'"):
        query.filter(value().field("score") >= 0.8)
    with pytest.raises(ArrayTypeError, match="Cannot compare"):
        array.filter(value().field("tokens") == "large")
    assert filtered._executor.metrics.queries == 0
    assert not array._backing.has_table
    result = await filtered.collect()
    assert result.rows == ((("refund",), {"quality": 0.95}),)


@pytest.mark.parametrize("operation", ["filter", "select", "limit", "aggregate", "join"])
async def test_direct_query_entry_points_forward_arguments_and_reuse_arrow(operation: str) -> None:
    array = evaluations()
    serialized = array._fxtr_serialize()
    expected_rows: tuple[tuple[tuple[str, ...], Mapping[str, object]], ...]
    match operation:
        case "filter":
            predicate = value().field("score") >= 0.8
            direct = array.filter(predicate)
            explicit = array.query().filter(predicate)
            expected_rows = ((("refund",), array[("refund",)]),)
        case "select":
            fields = {"case": dim("case"), "score": value().field("score")}
            direct = array.select(fields)
            explicit = array.query().select(fields)
            expected_rows = (
                (("booking",), {"case": "booking", "score": None}),
                (("delivery",), {"case": "delivery", "score": 0.7}),
                (("refund",), {"case": "refund", "score": 0.95}),
            )
        case "limit":
            order = [desc(value().field("tokens"))]
            direct = array.limit(2, order_by=order, ties="all")
            explicit = array.query().limit(2, order_by=order, ties="all")
            expected_rows = (
                (("delivery",), array[("delivery",)]),
                (("refund",), array[("refund",)]),
            )
        case "aggregate":
            aggregations = {"tickets": count(), "tokens": value().field("tokens").sum()}
            groups = {"model": value().field("model")}
            direct = array.aggregate(aggregations, group_by=groups)
            explicit = array.query().aggregate(aggregations, group_by=groups)
            expected_rows = (
                (("agent-v2",), {"tickets": 1, "tokens": 100}),
                (("agent-v3",), {"tickets": 2, "tokens": 200}),
            )
        case "join":
            models = Array.from_items(
                [(("agent-v3",), {"tier": "production"}), (("agent-v2",), {"tier": "legacy"})],
                ("[model: str]", struct_of(tier=str)),
            )
            on = [JoinKey(value().field("model"), dim("model"))]
            direct = array.join(models, on=on, left_name="evaluation", right_name="configuration")
            explicit = array.query().join(
                models, on=on, left_name="evaluation", right_name="configuration"
            )
            expected_rows = (
                (
                    ("booking",),
                    {"evaluation": array[("booking",)], "configuration": {"tier": "production"}},
                ),
                (
                    ("delivery",),
                    {"evaluation": array[("delivery",)], "configuration": {"tier": "legacy"}},
                ),
                (
                    ("refund",),
                    {"evaluation": array[("refund",)], "configuration": {"tier": "production"}},
                ),
            )
        case _:
            pytest.fail(f"Unknown query operation {operation!r}")
    assert isinstance(direct, ArrayQuery)
    assert direct.type == explicit.type
    assert direct._query == explicit._query
    assert direct._executor.metrics.queries == 0
    assert not array._backing.has_table
    result = await direct.collect()
    assert result.rows == expected_rows
    assert direct._executor.metrics.queries == 1
    table = array._arrow_table()
    assert await explicit.collect() == result
    assert array._backing.arrow_conversions == 1
    assert array._arrow_table() is table
    assert array._fxtr_serialize() == serialized


async def test_direct_limit_preserves_tie_policy() -> None:
    array = Array.from_items(
        [((1842,), 120), ((2077,), 120), ((3017,), 80)],
        ("[ticket: int]", int),
    )
    first = await array.limit(1, order_by=[desc(value())]).collect()
    all_ties = await array.limit(1, order_by=[desc(value())], ties="all").collect()
    assert first.rows == (((1842,), 120),)
    assert all_ties.rows == (((1842,), 120), ((2077,), 120))
    assert array._backing.arrow_conversions == 1


async def test_different_query_roots_share_the_immutable_arrays_arrow_cache() -> None:
    array = evaluations()
    first = await array.filter(value().field("score") >= 0.8).collect()
    table = array._arrow_table()
    second = (await array.aggregate({"tokens": value().field("tokens").sum()}).collect()).item()
    assert first.rows == ((("refund",), array[("refund",)]),)
    assert second == {"tokens": 300}
    assert array._backing.arrow_conversions == 1
    assert array._arrow_table() is table


async def test_new_array_rows_receive_a_separate_arrow_cache() -> None:
    original = Array.from_items([((1842,), 120)], ("[ticket: int]", int))
    assert (await original.query().aggregate({"tokens": value().sum()}).collect()).item() == {
        "tokens": 120
    }
    extended = Array.from_items(
        [*original.items(), ((2091,), 80)],
        original.type,
    )
    assert not extended._backing.has_table
    assert (await extended.query().aggregate({"tokens": value().sum()}).collect()).item() == {
        "tokens": 200
    }
    assert original._backing is not extended._backing
    assert original._backing.arrow_conversions == extended._backing.arrow_conversions == 1
    assert (await original.query().aggregate({"tokens": value().sum()}).collect()).item() == {
        "tokens": 120
    }


async def test_every_collect_executes_the_query_and_returns_its_own_array() -> None:
    array = evaluations()
    query = array.filter(value().field("score").is_not_null())
    results: list[Array[Evaluation]] = []

    async def collect() -> None:
        results.append(await query.collect())

    async with anyio.create_task_group() as tasks:
        for _ in range(8):
            tasks.start_soon(collect)
    results.append(await query.collect())

    assert all(result == results[0] for result in results)
    assert len({id(result) for result in results}) == 9
    assert query._executor.metrics.queries == 9
    # What is kept is on the immutable input: it is converted to Arrow once.
    assert array._backing.arrow_conversions == 1


async def test_concurrent_query_roots_prepare_once_with_shared_capacity() -> None:
    array = Array.from_items(
        [((ticket,), ticket) for ticket in range(1000)], ("[ticket: int]", int)
    )
    limiter = anyio.CapacityLimiter(2)
    results: dict[int, int] = {}

    async def query(threshold: int) -> None:
        result = (
            await array.query(query_limiter=limiter)
            .filter(value() >= threshold)
            .aggregate({"tickets": count()})
            .collect()
        ).item()
        results[threshold] = cast("int", result["tickets"])

    async with anyio.create_task_group() as tasks:
        for threshold in range(12):
            tasks.start_soon(query, threshold)
    assert results == {threshold: 1000 - threshold for threshold in range(12)}
    assert array._backing.arrow_conversions == 1
    assert limiter.borrowed_tokens == 0


async def test_a_query_built_on_a_collected_query_still_runs_the_whole_plan() -> None:
    array = evaluations()
    query = array.query().filter(value().field("score").is_not_null())
    await query.collect()

    downstream = query.aggregate({"tokens": value().field("tokens").sum()})

    # Collecting changed nothing about what later queries extend.
    assert len(downstream._query.sources) == 1 and downstream._query.sources[0] is array
    assert isinstance(downstream._query.plan, QueryAggregate)
    assert isinstance(downstream._query.plan.input, QueryFilter)
    assert (await downstream.collect()).item() == {"tokens": 220}
    assert query._query.sources[0]._backing.arrow_conversions == 1
    assert query._executor.metrics.queries == 2


async def test_collected_arrays_retain_arrow_and_python_presentation() -> None:
    query = evaluations().query().filter(value().field("score").is_not_null())
    array = await query.collect()
    assert type(array[("refund",)]) is dict
    # The way to use a result again: the returned array carries its Arrow table.
    backing = array._backing
    assert backing.has_table
    assert (await array.aggregate({"tokens": value().field("tokens").sum()}).collect()).item() == {
        "tokens": 220
    }
    assert backing.arrow_conversions == 0
    # Indexing decoded the one row it read.
    assert backing.python_materializations == 0 and not backing.has_rows


@pytest.mark.parametrize("direct_array", [False, True])
@pytest.mark.parametrize("direct_entry", [False, True])
async def test_local_join_accepts_concrete_arrays_and_local_queries(
    direct_array: bool, direct_entry: bool
) -> None:
    models = Array.from_items(
        [(("agent-v3",), {"tier": "production"}), (("agent-v2",), {"tier": "legacy"})],
        ("[model: str]", struct_of(tier=str)),
    )
    other = (
        evaluations()
        if direct_array
        else evaluations().query().filter(value().field("score").is_not_null())
    )
    joined = (models if direct_entry else models.query()).join(
        other, on=[(dim("model"), value().field("model"))]
    )
    result = (
        await joined.filter(value().field("left").field("tier") == "production")
        .limit(1, order_by=[desc(value().field("right").field("score"))])
        .select(
            {
                "tokens": value().field("right").field("tokens"),
                "tier": value().field("left").field("tier"),
            }
        )
        .collect()
    )
    assert result.rows == ((("agent-v3", "refund"), {"tokens": 120, "tier": "production"}),)


@dataclasses.dataclass(frozen=True, kw_only=True)
class QueryTicket(DataclassEntity, fxtr_type="tests.LocalQueryTicket"):
    subject: str


class TicketReferences(TypedDict):
    primary: BoundID[QueryTicket]
    related: list[BoundID[QueryTicket]]


async def test_nested_ids_remain_loadable_after_local_query_and_reuse() -> None:
    ticket = QueryTicket(subject="Refund request TKT-1842")
    storage = MemoryStorage()
    original = await storage.store(ticket)
    reference = original.entity_id.bind_type(QueryTicket)
    array = Array.from_items(
        [(("refund",), {"primary": reference, "related": [reference]})],
        ("[case: str]", TicketReferences),
    )
    queried = (
        await array.query()
        .select({"ticket": value().field("primary"), "related": value().field("related")})
        .collect()
    )
    bound = cast("BoundID[QueryTicket]", queried[("refund",)]["ticket"])
    related = cast("list[BoundID[QueryTicket]]", queried[("refund",)]["related"])
    assert bound.entity_id == related[0].entity_id == reference.entity_id
    assert await storage.load(bound) == ticket
    reused = await queried.query().filter(value().field("ticket") == reference).collect()
    assert await storage.load(cast("BoundID[QueryTicket]", reused[("refund",)]["ticket"])) == ticket


@pytest.mark.parametrize("direct_entry", [False, True])
async def test_an_id_constant_can_be_selected(direct_entry: bool) -> None:
    reference = QueryTicket(subject="Refund request TKT-1842").content_id()
    array = evaluations()
    selected = await (
        (array if direct_entry else array.query())
        .select({"ticket": LiteralExpression(reference, IdSchema())})
        .collect()
    )
    assert {row["ticket"] for row in selected.values()} == {reference}


@pytest.mark.parametrize("empty", [False, True])
async def test_empty_struct_and_nested_list_values_round_trip(empty: bool) -> None:
    schema = struct_of(
        metadata=struct_of(empty=struct_of(), history=list[float | None]),
        result=optional(struct_of()),
    )
    array = Array.from_items(
        []
        if empty
        else [
            (("refund",), {"metadata": {"empty": {}, "history": [0.7, None, 0.95]}, "result": {}}),
            (("booking",), {"metadata": {"empty": {}, "history": []}, "result": None}),
        ],
        ("[case: str]", schema),
    )
    result = await array.query().filter(dim("case").is_not_null()).collect()
    assert result == array
    assert result.content_id() == array.content_id()


async def test_identity_collect_is_free_and_scalar_limit_validates_cardinality() -> None:
    array = Array.scalar(1842, int)
    query = array.query()
    assert await query.collect() is array
    assert (await query.collect()).item() == 1842
    assert query._query.sources[0]._backing.arrow_conversions == 0
    assert (await array.limit(1).collect()).item() == 1842
    with pytest.raises(QueryError, match="exactly one row"):
        await array.limit(0).collect()


@pytest.mark.parametrize("maximum", ["rows", "bytes"])
async def test_local_result_limits_are_enforced(maximum: str) -> None:
    limits = DuckDBExecutionLimits(
        max_result_rows=1 if maximum == "rows" else 100,
        max_result_bytes=1 if maximum == "bytes" else 4096,
    )
    query = evaluations().query(limits=limits).select({"tokens": value().field("tokens")})
    with pytest.raises(DuckDBResourceLimitError, match="DuckDB result exceeded"):
        await query.collect()
    assert query._executor.query_limiter.borrowed_tokens == 0


async def test_deadline_includes_waiting_for_local_query_capacity() -> None:
    limiter = anyio.CapacityLimiter(1)
    borrower = object()
    limiter.acquire_on_behalf_of_nowait(borrower)
    array = evaluations()
    query = array.query(
        query_limiter=limiter, limits=DuckDBExecutionLimits(timeout_seconds=0.02)
    ).aggregate({"tickets": count()})
    try:
        with pytest.raises(
            DuckDBResourceLimitError, match="waited 0.02 seconds for query capacity"
        ):
            (await query.collect()).item()
    finally:
        limiter.release_on_behalf_of(borrower)
    assert not array._backing.has_table
    assert (
        await array.query(query_limiter=limiter).aggregate({"tickets": count()}).collect()
    ).item() == {
        "tickets": 3,
    }


async def test_unrepresentable_input_leaves_cache_empty_and_query_capacity_available() -> None:
    array = Array.from_items([((1842,), 2**70)], ("[ticket: int]", int))
    query = array.query().select({"value": value()})
    with pytest.raises(ArrowArrayError, match="outside Arrow int64 range"):
        await query.collect()
    assert not array._backing.has_table
    assert query._executor.query_limiter.borrowed_tokens == 0


async def test_local_input_and_output_span_arrow_batch_boundaries() -> None:
    row_count = 65_537
    array = Array.from_items(
        [((ticket,), ticket + 80) for ticket in range(row_count)],
        ("[ticket: int]", int),
    )
    result = await array.query().select({"tokens": value()}).collect()
    assert len(result) == row_count
    assert result[(0,)] == {"tokens": 80}
    assert result[(65_536,)] == {"tokens": 65_616}
    assert array._backing.has_table
    assert array._arrow_table().column(0).num_chunks == 2
    assert result._backing.has_table
    assert result._arrow_table().column(0).num_chunks >= 2
    assert (await result.query().aggregate({"tickets": count()}).collect()).item() == {
        "tickets": row_count
    }
    assert result._backing.arrow_conversions == 0


@pytest.mark.parametrize("direct_entry", [False, True])
async def test_invalid_queries_are_rejected_during_building(direct_entry: bool) -> None:
    array = evaluations()
    query = array if direct_entry else array.query()
    with pytest.raises(ArrayTypeError, match="Cannot compare"):
        query.filter(value().field("tokens") == "large")
    with pytest.raises(ArrayTypeError, match="No value field path"):
        query.select({"missing": value().field("not_a_field")})
    with pytest.raises(QueryError, match="non-negative 64-bit int"):
        query.limit(True)
    with pytest.raises(ArrayTypeError, match="non-null"):
        query.aggregate({"tickets": count()}, group_by={"score": value().field("score")})
    with pytest.raises(QueryError, match="equality key"):
        query.join(evaluations(), on=[])
    with pytest.raises(QueryError, match="concrete arrays or local queries"):
        query.join(cast("Any", object()), on=[(dim("case"), dim("case"))])


async def test_native_local_query_cancellation_releases_capacity_and_allows_reuse() -> None:
    left = Array.from_items([((case,), 1842) for case in range(50_000)], ("[case: int]", int))
    right = Array.from_items(
        [((replica,), 1842) for replica in range(50_000)],
        ("[replica: int]", int),
    )
    await left.query().limit(1).collect()
    await right.query().limit(1).collect()
    limiter = anyio.CapacityLimiter(1)
    query = (
        left.query(
            query_limiter=limiter,
            limits=DuckDBExecutionLimits(memory_limit="64MB", threads=1),
        )
        .join(right, on=[(value(), value())])
        .aggregate({"total": value().field("left").sum()})
    )
    cancelled = anyio.Event()
    scope = anyio.CancelScope()

    async def run() -> None:
        try:
            with scope:
                (await query.collect()).item()
                pytest.fail("The large native join finished before it was cancelled")
        finally:
            cancelled.set()

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run)
            while limiter.borrowed_tokens == 0:
                await anyio.sleep(0.001)
            await anyio.sleep(0.05)
            scope.cancel()
            await cancelled.wait()
    assert limiter.borrowed_tokens == 0
    assert (
        await left.query(query_limiter=limiter).aggregate({"tickets": count()}).collect()
    ).item() == {
        "tickets": 50_000,
    }


async def test_arrow_caches_do_not_keep_discarded_arrays_alive() -> None:
    array = evaluations()
    query = array.query().filter(value().field("score").is_not_null())
    result = await query.collect()
    original_ref = weakref.ref(array)
    result_ref = weakref.ref(result)
    del array, query, result
    # AnyIO worker threads retain their last returned result until the next job.
    await run_sync(gc.collect)
    gc.collect()
    assert original_ref() is None
    assert result_ref() is None


@pytest.mark.parametrize("direct_entry", [False, True])
async def test_row_selection_reuses_nested_values_with_multiple_dimensions(
    direct_entry: bool,
) -> None:
    storage = MemoryStorage()
    stored = await storage.store(QueryTicket(subject="Refund request TKT-1842"))
    reference = stored.entity_id
    schema = struct_of(
        model=str,
        score=int,
        metadata=struct_of(history=list[float | None], evidence=ListSchema(IdSchema())),
        error=optional(str),
    )
    array = Array.from_items(
        [
            (
                (model, sample),
                {
                    "model": "production",
                    "score": score,
                    "metadata": {"history": [0.7, None, 0.95], "evidence": [reference]},
                    "error": None,
                },
            )
            for model, sample, score in [
                ("agent-v3", 3, 95),
                ("agent-v2", 7, 70),
                ("agent-v3", 1, 95),
            ]
        ],
        ("[model: str, sample: int]", schema),
    )
    if direct_entry:
        array = await array.query().collect()
    query = (
        (array if direct_entry else array.query())
        .filter(value().field("model") == "production")
        .limit(1, order_by=[desc(value().field("score"))], ties="all")
        .filter(dim("sample") >= 1)
    )
    result = await query.collect()
    resolved = result._backing
    assert [key for key, _ in result.raw_rows] == [("agent-v3", 1), ("agent-v3", 3)]
    for key, row in result.raw_rows:
        assert row is array.raw_rows[array.row_position(key)][1]
        assert isinstance(row, frozendict)
        metadata = row["metadata"]
        assert isinstance(metadata, frozendict)
        evidence = metadata["evidence"]
        assert isinstance(evidence, tuple)
        assert evidence[0] is reference
    expected = Array.from_items(
        [(key, row) for key, row in array.raw_rows if key[0] == "agent-v3"], array.type
    )
    assert (await storage.store(result)).entity_id.cid == (
        await storage.store(expected)
    ).entity_id.cid
    assert resolved.has_table
    assert query._executor.metrics.arrow_bytes_written == result._arrow_table().nbytes
    assert (await result.aggregate({"selected": count()}).collect()).item() == {"selected": 2}
    assert resolved.arrow_conversions == 0


@pytest.mark.parametrize("limit_first", [False, True])
async def test_row_selection_preserves_filter_and_limit_order(limit_first: bool) -> None:
    array = evaluations()
    predicate = value().field("tokens") < 110
    query = array.query()
    query = (
        query.limit(1, order_by=[desc(value().field("tokens"))]).filter(predicate)
        if limit_first
        else query.filter(predicate).limit(1, order_by=[desc(value().field("tokens"))])
    )
    result = await query.collect()
    assert [key for key, _ in result.raw_rows] == ([] if limit_first else [("delivery",)])
    if result.raw_rows:
        assert result.raw_rows[0] is array.raw_rows[array.row_position(("delivery",))]


async def test_row_selection_crosses_input_and_result_batch_boundaries() -> None:
    row_count = 65_537
    array = Array.from_items(
        [
            ((ticket,), {"tokens": ticket + 80, "text": f"Ticket {ticket}"})
            for ticket in range(row_count)
        ],
        ("[ticket: int]", struct_of(tokens=int, text=str)),
    )
    result = await array.filter(value().field("tokens") >= 80).collect()
    assert len(result) == row_count
    assert result.raw_rows[0] is array.raw_rows[0]
    assert result.raw_rows[-1] is array.raw_rows[-1]
    assert result._backing.has_table
    assert result._arrow_table().column(0).num_chunks >= 2
    projected = await result.select({"tokens": value().field("tokens")}).collect()
    assert projected[(65_536,)] == {"tokens": 65_616}
    assert projected.raw_rows[-1][1] is not result.raw_rows[-1][1]
    assert result._backing.arrow_conversions == 0


@pytest.mark.parametrize("maximum", ["rows", "bytes"])
async def test_row_selection_limits_count_complete_values_and_all_ties(maximum: str) -> None:
    array = Array.from_items(
        [((ticket,), {"score": 95, "text": "Refund review " * 400}) for ticket in (1842, 2091)],
        ("[ticket: int]", struct_of(score=int, text=str)),
    )
    limits = DuckDBExecutionLimits(
        max_result_rows=1 if maximum == "rows" else 10,
        max_result_bytes=512 if maximum == "bytes" else 100_000,
    )
    query = array.query(limits=limits).limit(1, order_by=[desc(value().field("score"))], ties="all")
    unit = "rows" if maximum == "rows" else "Arrow bytes"
    with pytest.raises(DuckDBResourceLimitError, match=f"exceeded .* {unit}"):
        await query.collect()
    assert query._executor.metrics.queries == 0
    assert query._executor.query_limiter.borrowed_tokens == 0
    assert len(await array.query(limits=limits).limit(0).collect()) == 0
    assert array._backing.arrow_conversions == 1


async def test_row_selection_rejects_reuse_for_changed_values_before_preparation() -> None:
    array = evaluations()
    query = array.select({"tokens": value().field("tokens")})

    async def prepare() -> list[pa.Table]:
        pytest.fail("An invalid reuse request reached Arrow preparation")

    with pytest.raises(QueryError, match="Only filters, limits, and identity value selections"):
        await query._executor.execute_query(
            query._query.plan, [array.type], prepare, reuse_rows_from=array
        )
    assert not array._backing.has_table


async def test_collected_selection_does_not_retain_the_entire_python_input() -> None:
    array = evaluations()
    original_ref = weakref.ref(array)
    query = array.filter(value().field("tokens") >= 120)
    result = await query.collect()
    assert not result._backing.has_rows
    del array, query
    await run_sync(gc.collect)
    gc.collect()
    assert original_ref() is None
    assert result[("refund",)]["tokens"] == 120
    assert (await result.aggregate({"tokens": value().field("tokens").sum()}).collect()).item() == {
        "tokens": 120
    }


@pytest.mark.parametrize("operation", ["projection", "join"])
async def test_changed_values_preserve_nullable_structs_inside_nested_lists(operation: str) -> None:
    storage = MemoryStorage()
    ticket = QueryTicket(subject="Refund request TKT-1842")
    stored = await storage.store(ticket)
    reference = stored.entity_id
    schema = struct_of(
        model=str,
        events=list_of(
            struct_of(
                labels=list_of(optional(str)),
                metrics=optional(struct_of(quality=int, evidence=list_of(IdSchema()))),
                empty=struct_of(),
            )
        ),
    )
    array = Array.from_items(
        [
            (
                ("refund",),
                {
                    "model": "agent-v3",
                    "events": [
                        {
                            "labels": ["refund", None],
                            "metrics": {"quality": 95, "evidence": [reference]},
                            "empty": {},
                        },
                        {"labels": [], "metrics": None, "empty": {}},
                    ],
                },
            ),
            (("delivery",), {"model": "agent-v2", "events": []}),
        ],
        ("[case: str]", schema),
    )
    source_rows = [(key, cast("Mapping[str, Any]", row)) for key, row in array.raw_rows]
    if operation == "projection":
        query = array.query().select({"record": value()})
        expected = Array.from_items(
            [(key, {"record": row}) for key, row in source_rows],
            query.type,
        )
    else:
        lookup = Array.from_items(
            [
                ((2,), {"model": "agent-v2", "tier": "preview"}),
                ((3,), {"model": "agent-v3", "tier": "production"}),
                ((4,), {"model": "agent-v3", "tier": "production"}),
            ],
            ("[lookup: int]", struct_of(model=str, tier=str)),
        )
        query = (
            array.query()
            .join(lookup, on=[(value().field("model"), value().field("model"))])
            .select(
                {"record": value().field("left"), "tier": value().field("right").field("tier")},
            )
        )
        expected = Array.from_items(
            [
                ((*key, *right_key), {"record": row, "tier": right["tier"]})
                for key, row in source_rows
                for right_key, right in lookup.items()
                if row["model"] == right["model"]
            ],
            query.type,
        )
    result = await query.collect()
    assert result == expected
    assert (await storage.store(result)).entity_id.cid == (
        await storage.store(expected)
    ).entity_id.cid
    output_value = cast("Mapping[str, Any]", result.raw_rows[-1][1])
    first = cast("EntityID", output_value["record"]["events"][0]["metrics"]["evidence"][0])
    assert first == reference
    assert await storage.load(first.bind_type(QueryTicket)) == ticket
    assert result._backing.has_table
    assert (await result.aggregate({"n": count()}).collect()).item() == {"n": len(expected)}


@pytest.mark.parametrize("projection", ["whole", "fields", "chained"])
async def test_projection_keeps_nested_values_and_nulls(projection: str) -> None:
    array = Array.from_items(
        [
            ((1842,), {"metrics": {"score": 95}, "events": ["review", None]}),
            ((2091,), {"metrics": None, "events": []}),
        ],
        (
            "[ticket: int]",
            struct_of(metrics=optional(struct_of(score=int)), events=list_of(optional(str))),
        ),
    )
    if projection == "whole":
        query = array.select({"record": value()})
    else:
        query = array.select(
            {
                "metrics": value().field("metrics"),
                "events": value().field("events"),
                "score": value().field("metrics").field("score"),
            }
        )
        if projection == "chained":
            query = query.select({"record": value()})
    resolved = await query._run()
    assert not resolved._backing.has_rows
    table = resolved._arrow_table()
    result = await query.collect()
    for key, raw in result.raw_rows:
        original = cast("Mapping[str, Any]", array.raw_rows[array.row_position(key)][1])
        row = cast("Mapping[str, Any]", raw)
        record = row["record"] if projection in ("whole", "chained") else row
        if projection == "whole":
            assert record == original
        else:
            assert record["metrics"] == original["metrics"]
            assert record["events"] == original["events"]
            assert record["score"] == (None if original["metrics"] is None else 95)
    assert resolved._arrow_table() is table
    assert (await result.aggregate({"n": count()}).collect()).item() == {"n": 2}
    assert resolved._backing.arrow_conversions == 0


async def test_whole_value_selection_reuses_rows_and_field_selection_is_decoded() -> None:
    array = Array.from_items(
        [
            ((1842,), {"metrics": {"score": 95}, "events": ["review", None]}),
            ((2091,), {"metrics": None, "events": []}),
        ],
        (
            "[ticket: int]",
            struct_of(metrics=optional(struct_of(score=int)), events=list_of(optional(str))),
        ),
    )

    identity_query = array.select(value())
    identity_source = await identity_query._run()
    assert not identity_source._backing.has_rows
    identity = await identity_query.collect()
    assert identity == array
    assert all(
        selected is original
        for selected, original in zip(identity.raw_rows, array.raw_rows, strict=True)
    )

    events_query = array.select(value().field("events"))
    events_source = await events_query._run()
    assert not events_source._backing.has_rows
    events = await events_query.collect()
    for key, selected in events.raw_rows:
        original = cast("Mapping[str, Any]", array.raw_rows[array.row_position(key)][1])
        assert selected == original["events"]
    assert events_source._backing.has_table
    assert (await events.aggregate({"n": count()}).collect()).item() == {"n": 2}


@pytest.mark.parametrize("dimension_join", [False, True])
@pytest.mark.parametrize("project_inputs", [False, True])
@pytest.mark.parametrize("right_only", [False, True])
async def test_join_with_omitted_and_colliding_dimensions(
    dimension_join: bool, project_inputs: bool, right_only: bool
) -> None:
    schema = struct_of(model=str, history=list_of(optional(float)))
    left = Array.from_items(
        [(("agent-v3", 3), {"model": "agent-v3", "history": [0.7, None, 0.95]})],
        ("[model: str, sample: int]", schema),
    )
    right = Array.from_items(
        [
            ((11, "agent-v3", 3), {"model": "agent-v3", "history": [0.8]}),
            ((12, "agent-v3", 3), {"model": "agent-v3", "history": []}),
            ((13, "agent-v2", 3), {"model": "agent-v2", "history": [0.6]}),
        ],
        ("[sample: int, model: str, replica: int]", schema),
    )
    left_query, right_query = left.query(), right.query()
    if project_inputs:
        left_query = left_query.select({"record": value()})
        right_query = right_query.select({"record": value()})
    left_model = dim("model")
    if not dimension_join:
        left_model = value().field("record") if project_inputs else value()
        left_model = left_model.field("model")
    query = left_query.join(
        right_query,
        on=[(left_model, dim("model")), (dim("sample"), dim("replica"))],
        left_name="evaluation",
        right_name="configuration",
    )
    if right_only:
        query = query.select({"configuration": value().field("configuration")})
    resolved = await query._run()
    assert not resolved._backing.has_rows
    result = await query.collect()
    assert result.dim_names == ("model", "sample", "configuration_sample")
    assert len(result) == 2
    for key, raw in result.raw_rows:
        row = cast("Mapping[str, Any]", raw)
        configuration = row["configuration"]
        if project_inputs:
            configuration = configuration["record"]
        if not right_only:
            evaluation = row["evaluation"]
            if project_inputs:
                evaluation = evaluation["record"]
            assert evaluation == left.raw_rows[0][1]
        matched = right.raw_rows[right.row_position((key[2], key[0], key[1]))][1]
        assert configuration == matched
    assert (await result.aggregate({"n": count()}).collect()).item() == {"n": 2}
    assert resolved._backing.arrow_conversions == 0


async def test_scalar_projection_and_join() -> None:
    schema = struct_of(group=int, history=list_of(int))
    left = Array.scalar({"group": 7, "history": [80, 120]}, schema)
    right = Array.scalar({"group": 7, "history": []}, schema)
    result = (
        await left.join(right, on=[(value().field("group"), value().field("group"))])
        .select({"record": value().field("left")})
        .collect()
    )
    assert result.raw_rows[0][1] == {"record": left.raw_rows[0][1]}


@pytest.mark.parametrize("operation", ["projection", "join", "aggregate_projection"])
async def test_arrow_only_and_transformed_inputs_use_normal_decoding(operation: str) -> None:
    array = evaluations()
    if operation == "aggregate_projection":
        query = array.aggregate({"tokens": value().field("tokens").sum()}).select(
            {"report": value()}
        )
        intermediate = None
    else:
        parent = array.filter(value().field("score").is_not_null())
        intermediate = await parent._run()
        query = (
            parent.select({"record": value()})
            if operation == "projection"
            else parent.join(evaluations(), on=[(dim("case"), dim("case"))])
        )
    resolved = await query._run()
    assert not resolved._backing.has_rows
    if intermediate is not None:
        assert (
            not intermediate._backing.has_rows
            and intermediate._backing.python_materializations == 0
        )
    result = await query.collect()
    assert len(result) == (1 if operation == "aggregate_projection" else 2)
    assert (await result.aggregate({"n": count()}).collect()).item() == {"n": len(result)}
    if intermediate is not None:
        assert (
            not intermediate._backing.has_rows
            and intermediate._backing.python_materializations == 0
        )


@pytest.mark.parametrize("operation", ["projection", "join"])
async def test_collected_results_release_whole_input_arrays(operation: str) -> None:
    array = evaluations()
    other = evaluations()
    original_ref, other_ref = weakref.ref(array), weakref.ref(other)
    query = (
        array.select({"record": value()})
        if operation == "projection"
        else array.join(other, on=[(dim("case"), dim("case"))])
    )
    result = await query.collect()
    assert not result._backing.has_rows
    del array, other, query
    await run_sync(gc.collect)
    gc.collect()
    assert original_ref() is None and other_ref() is None
    assert len(result) == 3


async def test_deep_scalar_projection_keeps_batched_arrow_decoder() -> None:
    array = nested_array(32)
    query = array.select({"quality": leaf_column(32)})
    resolved = await query._run()
    assert not resolved._backing.has_rows
    assert (await query.collect()).raw_rows == tuple(
        ((i,), {"quality": None if i % 4 == 3 else i * 7 + 9}) for i in range(8)
    )


class PresentedHistory(TypedDict):
    model: str
    events: list[str]


async def test_results_do_not_share_mutable_presented_values() -> None:
    array = Array.from_items(
        [((1842,), {"model": "agent-v3", "events": ["reviewed"]})],
        ("[ticket: int]", PresentedHistory),
    )
    result = await array.select({"record": value()}).collect()
    raw = cast("Mapping[str, Any]", result.raw_rows[0][1])
    assert raw["record"] == array.raw_rows[0][1]
    presented = cast("Mapping[str, Any]", result[(1842,)])["record"]
    presented["model"] = "mutated"
    presented["events"].append("altered")
    assert array[(1842,)] == {"model": "agent-v3", "events": ["reviewed"]}
    assert result[(1842,)] == {"record": {"model": "agent-v3", "events": ["reviewed"]}}


async def test_large_join_intermediate_is_arrow_backed_until_small_final_collection() -> None:
    array = nested_array(16, rows=1000)
    lookup = Array.from_items(
        [((i,), {"group": i % 2, "tier": f"tier-{i}"}) for i in range(4)],
        ("[lookup: int]", struct_of(group=int, tier=str)),
    )
    joined = array.join(lookup, on=[(value().field("group"), value().field("group"))])
    intermediate = await joined._run()
    assert not intermediate._backing.has_rows and intermediate._backing.has_table
    assert len(intermediate) == 2000
    report = (
        await joined.select(
            {
                "tokens": value().field("left").field("tokens"),
                "tier": value().field("right").field("tier"),
            }
        )
        .limit(10)
        .aggregate({"n": count(), "tokens": value().field("tokens").sum()})
        .collect()
    )
    assert report.raw_rows == (((), {"n": 10, "tokens": 820}),)
    assert not intermediate._backing.has_rows and intermediate._backing.python_materializations == 0
    assert intermediate._backing.arrow_conversions == 0
    assert joined._executor.metrics.connections_opened == 1
    assert joined._executor.metrics.connections_reused == 1


def test_a_query_shows_its_type_and_plan() -> None:
    array = Array.from_items([((1842,), 80)], ("[ticket: int]", int))
    shown = repr(array.limit(1))
    assert shown.startswith("ArrayQuery(type=[ticket: int]: int, plan=QueryLimit(")
