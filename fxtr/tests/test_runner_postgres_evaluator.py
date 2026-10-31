"""The evaluator in the small: deriving node states at emission, evaluating a DAG view, and
answering observes over one live attempt."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from frozendict import frozendict

from fxtr.entity_defns.array import Array, ArrayType, rename_type
from fxtr.entity_defns.query_plan import PlannedQuery
from fxtr.entity_defns.schema import struct_of
from fxtr.runner import nodes
from fxtr.runner.nodes import NodeIdentity
from fxtr.runner.query_codec import encode_query
from fxtr.runner.session import AttemptError
from fxtr.storage.memory_storage import MemoryStorage

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres.evaluator import (  # noqa: E402
    Dag,
    MalformedEmissionError,
    NodeState,
    derive,
    evaluate,
)

pytestmark = pytest.mark.anyio

ROWS_TYPE = ArrayType.of(("[id: int]", struct_of(x=int, y=str)))
ROWS = Array.from_records([{"id": 1, "x": 10, "y": "p"}, {"id": 2, "x": 20, "y": "q"}], ROWS_TYPE)


def seeded(**inputs: Array) -> Dag:
    """A view holding one settled ``input`` node per argument."""
    storage = MemoryStorage()
    dag: Dag = {}
    for name, array in inputs.items():
        node = nodes.Input(name=name, array=storage.store_sync(array))
        dag[node.content_id()] = NodeState(node, array.type, value=array)
    return dag


def input_of(dag: Dag, name: str) -> nodes.Input:
    """The ``input`` node of the given argument name."""
    for state in dag.values():
        if isinstance(state.node, nodes.Input) and state.node.name == name:
            return state.node
    raise KeyError(name)


def one_step(
    input: NodeIdentity,
    type: ArrayType,
    build: Callable[[PlannedQuery[int]], PlannedQuery[int]],
) -> nodes.Query:
    """The query node a workflow records for one query step over one input of ``type``."""
    return nodes.Query(inputs=(input,), plan=encode_query(build(PlannedQuery.over(0, type)).plan))


def add(dag: Dag, node: nodes.Node, leaf: Array | None = None) -> NodeState:
    state = derive(node, dag, leaf)
    dag[node.content_id()] = state
    return state


def test_derive_types_a_derived_node_from_its_inputs_and_a_leaf_from_its_array() -> None:
    dag = seeded(rows=ROWS)
    (rows_id,) = dag
    field = add(dag, one_step(rows_id, ROWS_TYPE, lambda q: q.field("x")))
    assert field.type == ArrayType.of(("[id: int]", int)) and not field.settled
    source = Array.scalar(1, int)
    leaf = add(dag, nodes.SourceArray(name="one", array=MemoryStorage().store_sync(source)), source)
    assert leaf.type == source.type and leaf.value is source


def test_derive_rejects_inputs_and_unknown_input_nodes() -> None:
    dag = seeded(rows=ROWS)
    (rows_id,) = dag
    with pytest.raises(MalformedEmissionError):
        derive(nodes.Input(name="again", array=input_of(dag, "rows").array), dag, ROWS)
    unknown = one_step(rows_id, ROWS_TYPE, lambda q: q.field("y")).content_id()
    with pytest.raises(MalformedEmissionError):
        derive(one_step(unknown, ROWS_TYPE, lambda q: q.field("x")), dag, None)


def test_evaluate_computes_what_is_computable_and_repeats_until_settled() -> None:
    dag = seeded(rows=ROWS)
    (rows_id,) = dag
    field = add(dag, one_step(rows_id, ROWS_TYPE, lambda q: q.field("x")))
    renamed = add(
        dag, one_step(field.node.content_id(), field.type, lambda q: q.rename({"id": "k"}))
    )
    assert evaluate(dag) == []
    assert renamed.value == Array.from_items([((1,), 10), ((2,), 20)], ("[k: int]", int))
    # A second pass finds nothing to do and changes nothing.
    assert evaluate(dag) == [] and renamed.settled


def test_evaluate_records_a_raising_node_once_and_propagates_the_error_downstream() -> None:
    a = Array.from_items([((1,), 1)], ("[k: int]", int))
    b = Array.from_items([((2,), 2)], ("[k: int]", int))
    dag = seeded(a=a, b=b)
    a_id, b_id = dag
    combine = add(dag, nodes.Combine(inputs=frozendict({"a": a_id, "b": b_id}), join="exact"))
    downstream = add(dag, one_step(combine.node.content_id(), combine.type, lambda q: q.field("a")))
    failed = evaluate(dag)
    assert [identity for identity, _ in failed] == [combine.node.content_id()]
    assert combine.error is not None and combine.error.type_name == "ArrayError"
    assert combine.origin == combine.node.content_id()
    # The downstream node failed too, with the same error and origin, but is not reported again.
    assert downstream.error is combine.error and downstream.value is None
    assert downstream.origin == combine.node.content_id()
    assert evaluate(dag) == []


def test_evaluate_leaves_run_nodes_and_leaves_alone() -> None:
    dag = seeded(rows=ROWS)
    (rows_id,) = dag
    run = add(
        dag,
        nodes.RunStep(
            name="s",
            step_name="step",
            inputs=frozendict({"rows": rows_id}),
            map_over=(),
            join="exact",
            replicas=frozendict(),
            cache_key_prefix="root/s",
            type=ArrayType.of(int),
        ),
    )
    dependent = add(dag, one_step(run.node.content_id(), run.type, lambda q: q.rename({})))
    assert evaluate(dag) == []
    assert not run.settled and not dependent.settled


def query_over(*inputs: NodeIdentity, type: ArrayType = ROWS_TYPE) -> nodes.Query:
    """A query node over ``inputs`` whose SQL reads the first of them unchanged."""
    tables = {f"t{position}": PlannedQuery.over(position, type) for position in range(len(inputs))}
    planned = PlannedQuery.sql("SELECT t0.dims, t0.value FROM t0", tables=tables, output_type=type)
    return nodes.Query(inputs=inputs, plan=encode_query(planned.plan))


def test_evaluate_runs_a_query_whose_inputs_have_values_and_what_reads_it() -> None:
    dag = seeded(rows=ROWS)
    rows_id = input_of(dag, "rows").content_id()
    query = add(dag, query_over(rows_id, rows_id))  # one input, read at two positions
    renamed = add(
        dag, one_step(query.node.content_id(), ROWS_TYPE, lambda q: q.rename({"id": "k"}))
    )
    assert evaluate(dag) == []
    assert query.value == ROWS
    assert renamed.value == Array.from_items(ROWS.raw_rows, rename_type(ROWS_TYPE, {"id": "k"}))


def run_node(dag: Dag, input_id: NodeIdentity) -> NodeState:
    return add(
        dag,
        nodes.RunStep(
            name="s",
            step_name="step",
            inputs=frozendict({"rows": input_id}),
            map_over=(),
            join="exact",
            replicas=frozendict(),
            cache_key_prefix="root/s",
            type=ROWS_TYPE,
        ),
    )


def test_a_query_waits_for_an_unsettled_input_and_takes_a_failed_inputs_error() -> None:
    dag = seeded(rows=ROWS)
    run = run_node(dag, input_of(dag, "rows").content_id())
    query = add(dag, query_over(run.node.content_id()))
    assert evaluate(dag) == [] and not query.settled

    run.error = AttemptError.from_exception(RuntimeError("boom"))
    run.origin = run.node.content_id()
    assert evaluate(dag) == []
    assert query.error == run.error and query.origin == run.origin
