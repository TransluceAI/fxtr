"""A workflow's query nodes, run as Postgres jobs: what a workflow records for the queries it
collects, and what they compute.

Every job runs with determinism verification, so each workflow function runs twice and must
record the same nodes both times.
"""

from __future__ import annotations

import dataclasses
from typing import Any, TypedDict

import anyio
import pytest
from postgres_testing import a_client

import fxtr
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.query import dim, value
from fxtr.entity_defns.schema import IdSchema, struct_of
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.runner import nodes
from fxtr.runner.postgres.database import Database
from fxtr.runner.postgres.ledger import NodeRecord

pytestmark = pytest.mark.anyio

SCORES = Array.from_items([((1,), 0.5), ((2,), 1.5)], ("[id: int]", float))

BODY_CALLS = [0]  # how many times a counting workflow function has been entered, across its runs


def next_call() -> int:
    BODY_CALLS[0] += 1
    return BODY_CALLS[0]


@dataclasses.dataclass(frozen=True)
class Ran:
    """A succeeded job: its result, the root workflow's nodes in creation order, and the store
    its entities are in."""

    result: Array[Any]
    nodes: list[NodeRecord]
    storage: Any


async def run(
    database: Database, root: Any, *functions: Any, inputs: dict[str, Array[Any]] | None = None
) -> Ran:
    async with a_client(database, root, *functions, verify_determinism=True) as client:
        status = await client.run(root, dict(inputs or {}))
        assert status.state == "succeeded", status
        described = await client.jobs.describe(status.job_id)
        return Ran(
            await client.jobs.result(status.job_id),
            described.invocations["root"].nodes,
            client.entities,
        )


def query_nodes(record: Ran) -> list[tuple[type[nodes.Node], tuple[Any, ...]]]:
    """Each node of the root workflow, in order: its class, and its inputs if it is a query."""
    return [
        (type(entry.node), entry.node.inputs if isinstance(entry.node, nodes.Query) else ())
        for entry in record.nodes
    ]


@workflow(inputs={"scores": ("[id: int]", float)}, output=("[id: int]", float))
async def workflow_query(
    context: WorkflowContext, scores: ArrayHandle[float]
) -> ArrayHandle[float]:
    return scores.query().filter(value() >= 1).select(value() * 2).collect()


async def test_a_workflow_query_chain_is_one_node(database: Database) -> None:
    record = await run(database, workflow_query, inputs={"scores": SCORES})
    assert record.result == Array.from_items([((2,), 3.0)], ("[id: int]", float))
    (source, query) = record.nodes
    assert isinstance(query.node, nodes.Query) and query.node.inputs == (source.identity,)


@workflow(
    inputs={"scores": ("[id: int]", float)},
    output=("[id: int]", struct_of(doubled=float, tripled=float)),
)
async def branched_workflow_query(
    context: WorkflowContext, scores: ArrayHandle[float]
) -> ArrayHandle[Any]:
    eligible = scores.query().filter(value() >= 1).collect()
    doubled = eligible.query().select(value() * 2).collect()
    tripled = eligible.query().select(value() * 3).collect()
    return context.combine({"doubled": doubled, "tripled": tripled})


async def test_a_collected_query_is_one_node_two_later_queries_read(database: Database) -> None:
    record = await run(database, branched_workflow_query, inputs={"scores": SCORES})
    assert record.result == Array.from_records(
        [{"id": 2, "doubled": 3.0, "tripled": 4.5}],
        ("[id: int]", struct_of(doubled=float, tripled=float)),
    )
    source, shared, *_ = record.nodes
    assert query_nodes(record) == [
        (nodes.Input, ()),
        (nodes.Query, (source.identity,)),
        (nodes.Query, (shared.identity,)),
        (nodes.Query, (shared.identity,)),
        (nodes.Combine, ()),
    ]


class Point(TypedDict):
    x: int


POINTS_TYPE = ArrayType.of(("[k: int]", struct_of(x=int)))


@workflow(inputs={"points": POINTS_TYPE}, output=("[which: str, k: int]", struct_of(x=int)))
async def alias_before_its_query(
    context: WorkflowContext, points: ArrayHandle[Any]
) -> ArrayHandle[Any]:
    kept = points.query().filter(dim("k") > 0)
    alias = kept.retype(("[k: int]", Point)).collect()  # presentation only
    return context.stack({"alias": alias, "kept": kept.collect()}, dim="which")


async def test_a_query_and_its_presentation_alias_are_one_node(database: Database) -> None:
    """The alias, collected first, records the query; collecting the query itself records the
    same description under another token, which the host recognizes as the node it has."""
    points = Array.from_records([{"k": 0, "x": 1}, {"k": 1, "x": 2}], POINTS_TYPE)
    record = await run(database, alias_before_its_query, inputs={"points": points})
    assert record.result == Array.from_items(
        [(("alias", 1), {"x": 2}), (("kept", 1), {"x": 2})],
        ("[which: str, k: int]", struct_of(x=int)),
    )
    assert [kind for kind, _ in query_nodes(record)] == [nodes.Input, nodes.Query, nodes.Stack]


MATRIX_TYPE = ArrayType.of(("[row: int, column: str]", float))
REORDERED_TYPE = ArrayType.of(("[column: str, row: int]", float))
MATRIX = Array.from_items(
    [((1, "latency"), 120.0), ((1, "cost"), 0.4), ((2, "latency"), 95.0)], MATRIX_TYPE
)


@workflow(inputs={"matrix": MATRIX_TYPE}, output=REORDERED_TYPE)
async def reordered_then_filtered(
    context: WorkflowContext, matrix: ArrayHandle[float]
) -> ArrayHandle[float]:
    return matrix.retype(REORDERED_TYPE).query().filter(dim("column") == "latency").collect()


@workflow(inputs={"matrix": MATRIX_TYPE}, output=REORDERED_TYPE)
async def reordered(context: WorkflowContext, matrix: ArrayHandle[float]) -> ArrayHandle[float]:
    return matrix.retype(REORDERED_TYPE)


async def test_a_retype_that_reorders_dims_is_kept_by_the_query_after_it_and_on_return(
    database: Database,
) -> None:
    record = await run(database, reordered_then_filtered, inputs={"matrix": MATRIX})
    assert record.result.type == REORDERED_TYPE
    assert record.result == Array.from_items(
        [(("latency", 1), 120.0), (("latency", 2), 95.0)], REORDERED_TYPE
    )
    record = await run(database, reordered, inputs={"matrix": MATRIX})
    assert record.result.type == REORDERED_TYPE
    assert record.result == MATRIX.retype(REORDERED_TYPE)
    assert [kind for kind, _ in query_nodes(record)] == [nodes.Input, nodes.Query]


MODEL_TYPE = ArrayType.of(("[model_id: int]", struct_of(name=str)))
MODEL_SCORE_TYPE = ArrayType.of(("[model_id: int]", int))
MODEL_REPORT_TYPE = ArrayType.of(("[model_id: int]", struct_of(name=str, score=int)))


@workflow(inputs={"models": MODEL_TYPE, "scores": MODEL_SCORE_TYPE}, output=MODEL_REPORT_TYPE)
async def workflow_sql_join(
    context: WorkflowContext, models: ArrayHandle[Any], scores: ArrayHandle[int]
) -> ArrayHandle[Any]:
    return fxtr.sql(
        "SELECT m.dims AS dims, {name: m.value.name, score: s.value} AS value "
        "FROM models AS m JOIN scores AS s ON m.dims.model_id = s.dims.model_id",
        tables={"models": models, "scores": scores.query().filter(value() > 0)},
        output_type=MODEL_REPORT_TYPE,
    ).collect()


async def test_workflow_sql_over_two_handles_and_a_query_is_one_node(database: Database) -> None:
    models = Array.from_records(
        [{"model_id": 1, "name": "small"}, {"model_id": 2, "name": "large"}], MODEL_TYPE
    )
    scores = Array.from_items([((1,), 0), ((2,), 91)], MODEL_SCORE_TYPE)
    record = await run(database, workflow_sql_join, inputs={"models": models, "scores": scores})
    assert record.result == Array.from_records(
        [{"model_id": 2, "name": "large", "score": 91}], MODEL_REPORT_TYPE
    )
    first, second, _ = record.nodes
    assert query_nodes(record)[-1] == (nodes.Query, (first.identity, second.identity))


NOTES_TYPE = ArrayType.of(("[k: int]", IdSchema()))


@workflow(inputs={}, output=NOTES_TYPE)
async def workflow_sql_over_ids(context: WorkflowContext) -> ArrayHandle[Any]:
    note = await context.store_raw_data({"$type": "tests.Note", "text": "kept"})
    other = await context.store_raw_data({"$type": "tests.Note", "text": "dropped"})
    notes = context.add_source_array(
        "notes", Array.from_items([((1,), other), ((2,), note)], NOTES_TYPE)
    )
    return fxtr.sql(
        "SELECT dims, value FROM notes WHERE dims.k > 1", tables={"notes": notes}
    ).collect()


async def test_workflow_sql_over_entity_ids_restores_them(database: Database) -> None:
    """User SQL sees tokens in place of entity IDs; a workflow's query node derives its value on
    the calling thread, so the substitution must not rely on running in a worker thread."""
    record = await run(database, workflow_sql_over_ids)
    ((key, note),) = record.result.items()
    assert key == (2,)
    assert await record.storage.load_raw_data(note) == {"$type": "tests.Note", "text": "kept"}


@step(inputs={"scores": ("[id: int]", float)}, output=("[id: int]", float))
async def boost_query_result(context: StepContext, scores: Array[float]) -> Array[float]:
    return await scores.select(value() + 1).collect()


@workflow(inputs={"scores": ("[id: int]", float)}, output=("[id: int]", float))
async def query_before_step(
    context: WorkflowContext, scores: ArrayHandle[float]
) -> ArrayHandle[float]:
    kept = scores.query().filter(value() >= 1).collect()
    return context.run_step("boost", boost_query_result, {"scores": kept})


async def test_a_workflow_query_feeds_a_step_as_one_node(database: Database) -> None:
    record = await run(database, query_before_step, boost_query_result, inputs={"scores": SCORES})
    assert record.result == Array.from_items([((2,), 2.5)], ("[id: int]", float))
    assert [kind for kind, _ in query_nodes(record)] == [nodes.Input, nodes.Query, nodes.RunStep]


# ---- determinism verification ----


@workflow(
    inputs={"scores": ("[id: int]", float)},
    output=("[id: int]", struct_of(doubled=float, tripled=float)),
)
async def queries_concurrently(
    context: WorkflowContext, scores: ArrayHandle[float]
) -> ArrayHandle[Any]:
    # Two branches build on one query and each waits for its own result, in an order that
    # depends on scheduling: what they record must not.
    kept = scores.query().filter(value() >= 1)
    handles: dict[str, ArrayHandle[float]] = {}

    async def branch(name: str, factor: float, delay: float) -> None:
        await anyio.sleep(delay)
        handles[name] = kept.select(value() * factor).collect()
        await handles[name].observe()

    async with anyio.create_task_group() as tg:
        tg.start_soon(branch, "doubled", 2.0, 0.01 if next_call() % 2 else 0.0)
        tg.start_soon(branch, "tripled", 3.0, 0.0)
    return context.combine(handles)


async def test_verification_passes_queries_built_on_one_query_in_either_order(
    database: Database,
) -> None:
    BODY_CALLS[0] = 0
    record = await run(database, queries_concurrently, inputs={"scores": SCORES})
    assert BODY_CALLS[0] == 2  # the two runs started the branches in opposite orders
    assert record.result == Array.from_records(
        [{"id": 2, "doubled": 3.0, "tripled": 4.5}],
        ("[id: int]", struct_of(doubled=float, tripled=float)),
    )
    (source, *_) = record.nodes
    queries = [inputs for kind, inputs in query_nodes(record) if kind is nodes.Query]
    assert queries == [(source.identity,), (source.identity,)]
