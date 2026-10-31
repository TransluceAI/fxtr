"""Tests for nodes: identities, edges and names, and the relational round trip."""

from __future__ import annotations

import json
from typing import Any

import pytest
from frozendict import frozendict

from fxtr.core.entity_data import EntityID
from fxtr.duckdb_engine.local_query import query_arrays
from fxtr.entity_defns.array import Array, ArrayType, ArrayTypeError, Key
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.query import dim, value
from fxtr.entity_defns.query_plan import PlannedQuery
from fxtr.entity_defns.schema import IdSchema, optional, struct_of
from fxtr.runner import nodes
from fxtr.runner.nodes import (
    KINDS,
    NodeRow,
    NodeRowError,
    from_row,
    kind_of,
    node_inputs,
    node_name,
    node_type,
    to_row,
)
from fxtr.runner.query_codec import encode_query
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio

PROMPTS = ArrayType.of(("[prompt: int]", str))
SCORES = ArrayType.of(("[prompt: int, sample: int]", float))
ROWS = ArrayType.of(("[id: int]", struct_of(x=int, y=str)))


def fake_identity(n: int) -> EntityID:
    return EntityID(cid=b"\x01\x71\x12\x20" + n.to_bytes(32, "big"))


A, B, C, D = (fake_identity(n) for n in (1, 2, 3, 4))


def prompts() -> Array[str]:
    return Array.from_items([((0,), "a"), ((1,), "b")], PROMPTS)


def run_step(**overrides: Any) -> nodes.RunStep:
    fields: dict[str, Any] = dict(
        name="score",
        step_name="tests.score",
        inputs=frozendict({"sample": A, "n": B}),
        map_over=("prompt", "sample"),
        join="exact",
        replicas=frozendict({"rep": (0, 1, 2)}),
        cache_key_prefix="root/score",
        type=ArrayType.of(("[prompt: int, sample: int, rep: int]", float)),
    )
    fields.update(overrides)
    return nodes.RunStep(**fields)


ROWS_FILTER = encode_query(PlannedQuery.over(0, ROWS).filter(value().field("x") >= 0).plan)
"""A plan reading one source: what a query node carries, encoded as it is on the wire."""


async def every_node(storage: MemoryStorage | None = None) -> list[nodes.Node]:
    storage = storage or MemoryStorage()
    array = await storage.store(prompts())
    return [
        nodes.Input(name="prompts", array=array),
        nodes.SourceArray(name="src", array=array),
        nodes.Combine(inputs=frozendict({"a": A, "b": B}), join="outer"),
        nodes.Stack(inputs=((1, A), (3, B)), dim="k"),
        nodes.Query(inputs=(C, A), plan=ROWS_FILTER),
        run_step(),
        nodes.RunChildWorkflow(
            name="child",
            workflow_name="tests.child",
            inputs=frozendict({"prompts": A}),
            map_over=(),
            join="inner",
            replicas=frozendict(),
            cache_key_prefix="root/child",
            type=ArrayType.of(("[k: int]", IdSchema())),
        ),
    ]


# ---- identity ----


def test_identity_is_the_cid_of_the_entity_form_and_ignores_mapping_order() -> None:
    one = nodes.Combine(inputs=frozendict({"a": A, "b": B}), join="exact")
    other = nodes.Combine(inputs=frozendict({"b": B, "a": A}), join="exact")
    assert one.content_id() == other.content_id()
    assert len(one.content_id().cid) == 36
    # Two equal nodes built independently agree; nothing is stored to get there.
    assert run_step().content_id() == run_step().content_id()


def test_identity_changes_with_inputs_parameters_and_declared_type() -> None:
    base = run_step().content_id()
    changed = [
        run_step(inputs=frozendict({"sample": A, "n": C})),
        run_step(map_over=("prompt",)),
        run_step(replicas=frozendict()),
        run_step(cache_key_prefix="elsewhere/score"),
        run_step(name="score2"),
        run_step(type=ArrayType.of(("[prompt: int, sample: int, rep: int]", optional(float)))),
    ]
    identities = {n.content_id() for n in changed}
    assert base not in identities
    assert len(identities) == len(changed)


async def test_leaf_identity_follows_the_array_content() -> None:
    storage = MemoryStorage()
    s1 = nodes.SourceArray(name="s", array=await storage.store(prompts()))
    s2 = nodes.SourceArray(
        name="s", array=await storage.store(Array.from_items([((0,), "z")], PROMPTS))
    )
    s3 = nodes.SourceArray(name="t", array=await storage.store(prompts()))
    assert len({s1.content_id(), s2.content_id(), s3.content_id()}) == 3
    # The same array under a different kind is a different node.
    assert nodes.Input(name="s", array=s1.array).content_id() != s1.content_id()


# ---- types ----


async def test_node_type_is_derived_from_contents() -> None:
    types = {A: ROWS, B: ROWS, C: ROWS, D: PROMPTS}
    storage = MemoryStorage()
    array = await storage.store(prompts())

    def input_type(identity: EntityID) -> Any:
        return types[identity]

    def array_type(ref: Any) -> Any:
        assert ref == array
        return PROMPTS

    struct = struct_of(x=int, y=str)
    for node, expected in [
        (nodes.Input(name="p", array=array), PROMPTS),
        (nodes.SourceArray(name="p", array=array), PROMPTS),
        (
            nodes.Combine(inputs=frozendict({"a": A, "b": B}), join="exact"),
            ArrayType.of(("[id: int]", struct_of(a=struct, b=struct))),
        ),
        (
            nodes.Stack(inputs=(("one", A), ("two", B)), dim="copy"),
            ArrayType.of(("[copy: str, id: int]", struct)),
        ),
        (nodes.Query(inputs=(C,), plan=ROWS_FILTER), ROWS),
        (run_step(), run_step().type),
    ]:
        assert node_type(node, input_type, array_type) == expected, node
    with pytest.raises(ArrayTypeError):
        node_type(nodes.Stack(inputs=(("one", A), ("two", D)), dim="copy"), input_type, array_type)


# ---- edges and names ----


async def test_edges_and_names() -> None:
    by_kind = {kind_of(n): n for n in await every_node()}
    assert set(by_kind) == set(KINDS.values())
    assert node_inputs(by_kind["combine"]) == (("a", A), ("b", B))
    assert node_inputs(by_kind["run_step"]) == (("sample", A), ("n", B))
    assert node_inputs(by_kind["input"]) == ()
    assert node_inputs(by_kind["source_array"]) == ()
    assert node_inputs(by_kind["stack"]) == ((1, A), (3, B))
    assert node_inputs(by_kind["query"]) == ((0, C), (1, A))
    assert {kind: node_name(n) for kind, n in by_kind.items()} == {
        "input": None,
        "source_array": "src",
        "combine": None,
        "stack": None,
        "query": None,
        "run_step": "score",
        "run_child_workflow": "child",
    }


# ---- expansion and collection ----


def test_expand_slices_inputs_per_joint_key_and_repeats_them_per_replica() -> None:
    node = run_step(
        map_over=("prompt",),
        replicas=frozendict({"rep": (0, 1)}),
        type=ArrayType.of(("[prompt: int, rep: int]", float)),
    )
    sample = Array.from_items([((0,), "x"), ((1,), "y")], ("[prompt: int]", str))
    n = Array.scalar(3, int)  # lacks the mapped dim: passed whole
    expansion = nodes.expand(node, {"sample": sample, "n": n})
    assert [d.name for d in expansion.dims] == ["prompt"]
    assert [s.key for s in expansion.spawns] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    first = expansion.spawns[0]
    assert first.mapped == {"prompt": 0, "rep": 0}
    assert first.inputs["sample"] == Array.scalar("x", str)
    assert first.inputs["n"] is n
    # Nothing mapped or replicated over: one spawn with the empty key and the inputs whole.
    plain = run_step(map_over=(), replicas=frozendict(), type=ArrayType.of(float))
    (only,) = nodes.expand(plain, {"sample": sample, "n": n}).spawns
    assert only.key == () and only.mapped == {} and only.inputs["sample"] is sample


@pytest.mark.parametrize("join", ["inner", "outer"])
@pytest.mark.parametrize("arrow_backed", [False, True])
def test_expand_ragged_inputs_in_mapped_order_with_broadcasts(
    join: str, arrow_backed: bool
) -> None:
    """Each spawn's input is the array ``sel`` gives at its key, with the same content ID (the
    step request, and so its cache key, is built from it), whichever form the input holds."""
    sample = Array.from_items(
        [((0, "a", 1), 10), ((0, "b", 1), 11), ((1, "a", 2), 20)],
        ("[prompt: int, detail: str, batch: int]", int),
    )
    other = Array.from_items([((1, 0), 100), ((3, 0), 300)], ("[batch: int, row: int]", int))
    scalar = Array.scalar(7, int)
    inputs = {"sample": sample, "n": other, "scalar": scalar}
    if arrow_backed:
        inputs = {
            name: query_arrays(PlannedQuery.over(0, array.type).plan, [array])
            for name, array in inputs.items()
        }
    node = run_step(map_over=("batch", "prompt"), join=join)
    expansion = nodes.expand(node, inputs)
    assert expansion.spawns
    for spawn in expansion.spawns:
        for name, array in inputs.items():
            key = {d: spawn.mapped[d] for d in node.map_over if d in array.dim_names}
            expected = query_arrays(PlannedQuery.over(0, array.type).sel(key).plan, [array])
            assert spawn.inputs[name] == expected
            assert spawn.inputs[name].content_id() == expected.content_id()
        assert spawn.inputs["scalar"] is inputs["scalar"]
    # Replicas share their slices; broadcasting one batch across prompts also shares it.
    assert expansion.spawns[0].inputs["sample"] is expansion.spawns[1].inputs["sample"]


def test_expand_empty_input_and_unmapped_empty_array() -> None:
    empty = Array.from_items([], ("[prompt: int]", int))
    node = run_step(map_over=("prompt",), replicas=frozendict())
    assert nodes.expand(node, {"sample": empty}).spawns == ()
    plain = run_step(map_over=(), replicas=frozendict())
    (spawn,) = nodes.expand(plain, {"sample": empty}).spawns
    assert spawn.inputs["sample"] is empty


def test_collect_stacks_the_spawns_results_into_the_declared_type() -> None:
    node = run_step(
        map_over=("prompt",),
        replicas=frozendict({"rep": (0, 1)}),
        type=ArrayType.of(("[prompt: int, rep: int]", float)),
    )
    values: dict[Key, Array[Any]] = {
        (0, 0): Array.scalar(0.0, float),
        (0, 1): Array.scalar(0.5, float),
        (1, 0): Array.scalar(1.0, float),
        (1, 1): Array.scalar(1.5, float),
    }
    collected = nodes.collect(node, values)
    assert collected.type == node.type
    assert collected == Array.from_items(
        [((0, 0), 0.0), ((0, 1), 0.5), ((1, 0), 1.0), ((1, 1), 1.5)], node.type
    )
    # An empty key set gives an empty array of the declared type; no mapping gives the one value.
    assert nodes.collect(node, {}) == Array(
        dims=node.type.dims, schema=node.type.schema, raw_rows=()
    )
    plain = run_step(map_over=(), replicas=frozendict(), type=ArrayType.of(float))
    assert nodes.collect(plain, {(): Array.scalar(2.0, float)}) == Array.scalar(2.0, float)


# ---- relational round trip ----


async def test_every_kind_round_trips_through_its_row_with_the_same_identity() -> None:
    for node in await every_node():
        row = to_row(node)
        assert row.kind == kind_of(node)
        json.dumps(row.params)  # params are plain JSON
        assert not ({"inputs", "input", "$type"} & set(row.params))
        back = from_row(row)
        # Compare wire forms: a BoundID's equality includes its loader, and a stored reference
        # and a decoded one resolve theirs from different annotations.
        assert DEFAULT_CONVERTER.unstructure_entity(back) == DEFAULT_CONVERTER.unstructure_entity(
            node
        ), row
        assert back.content_id() == node.content_id()


async def test_row_params_are_readable() -> None:
    storage = MemoryStorage()
    row = to_row(run_step())
    assert row.params == {
        "name": "score",
        "step_name": "tests.score",
        "map_over": ["prompt", "sample"],
        "join": "exact",
        "replicas": {"rep": [0, 1, 2]},
        "cache_key_prefix": "root/score",
        "type": {
            "dims": [["prompt", "int"], ["sample", "int"], ["rep", "int"]],
            "schema": "float",
        },
    }
    leaf = to_row(nodes.Input(name="prompts", array=await storage.store(prompts())))
    assert set(leaf.params) == {"name", "array"}
    assert isinstance(leaf.params["array"], str)


def test_malformed_rows_are_rejected() -> None:
    with pytest.raises(NodeRowError):
        from_row(NodeRow(kind="nope", params={}, inputs=()))
    with pytest.raises(NodeRowError):
        from_row(NodeRow(kind="query", params={"plan": ""}, inputs=((1, A),)))  # not from 0
    with pytest.raises(NodeRowError):
        from_row(NodeRow(kind="input", params={"name": "p", "array": 5}, inputs=()))
    with pytest.raises(NodeRowError):
        from_row(NodeRow(kind="combine", params={"join": "sideways"}, inputs=(("a", A),)))
    with pytest.raises(NodeRowError):
        from_row(
            NodeRow(kind="source_array", params={"name": "s", "array": "x"}, inputs=(("a", A),))
        )


async def test_a_query_node_holding_entity_ids_is_computed_on_the_caller_thread() -> None:
    """A query node's plan runs inline, so the reference checks its result needs must not ask for
    a cancellation that only an anyio worker thread can answer."""
    storage = MemoryStorage()
    referenced = storage.store_sync(Array.scalar(1, int))
    ids_type = ArrayType.of(("[id: int]", IdSchema()))
    ids = Array.from_items([((1,), referenced.entity_id), ((2,), referenced.entity_id)], ids_type)
    plan = encode_query(PlannedQuery.over(0, ids_type).filter(dim("id") >= 2).plan)

    computed = nodes.compute(nodes.Query(inputs=(A,), plan=plan), {0: ids})
    assert computed == Array.from_items([((2,), referenced.entity_id)], ids_type)
