"""Tests for the runner's workflow and step contexts and its handle, against a mocked session.

The contexts' job is to compute types and describe nodes to the session with the ``emit_<kind>``
methods, naming each with a fresh token; the assertions here are on those calls, including which
workflow queries become nodes and when. What the host builds from them is the assembler's business
(``test_runner_assembly.py``).
"""

from __future__ import annotations

from typing import Any, TypedDict, cast
from unittest.mock import create_autospec

import pytest
from frozendict import frozendict

import fxtr
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ArrayType, ArrayTypeError, ArrayTypeSpec
from fxtr.entity_defns.query import dim, value
from fxtr.entity_defns.query_plan import PlannedQuery, QueryPlan
from fxtr.entity_defns.schema import needs_presentation, optional, struct_of
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.signatures import SignatureError
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.runner.contexts import (
    DuplicateNodeNameError,
    ForeignArrayHandleError,
    RunnerArrayHandle,
    RunnerStepContext,
    RunnerWorkflowContext,
    node_token_of,
)
from fxtr.runner.query_codec import decode_query
from fxtr.runner.session import (
    AttemptID,
    NodeToken,
    StepAssignment,
    StepRunSession,
    WorkflowAssignment,
    WorkflowRunSession,
)
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio

ATTEMPT = AttemptID("attempt-1")
PROMPTS = ArrayType.of(("[prompt: int]", str))
SCORES = ArrayType.of(("[prompt: int, sample: int]", float))
FAKE_ID = EntityID(cid=b"\x01\x71\x12\x20" + bytes(32))

# The host numbers an assignment's inputs from zero; the context's own tokens follow.
IN0, IN1 = NodeToken(0), NodeToken(1)


def T(n: int) -> NodeToken:
    return NodeToken(n)


@step(inputs={"prompt": str, "n": int}, output=("[sample: int]", str))
async def sample(context: StepContext, prompt: str, n: int) -> Array[str]:
    raise NotImplementedError


@step(inputs={"samples": ("[sample: int]", str)}, output=int)
async def count(context: StepContext, samples: Array[str]) -> int:
    raise NotImplementedError


@workflow(inputs={"prompt": str}, output=("[sample: int]", float))
async def score_prompt(context: WorkflowContext, prompt: ArrayHandle[str]) -> ArrayHandle[float]:
    raise NotImplementedError


def make_session(protocol: Any = WorkflowRunSession) -> Any:
    """A mocked run session with a real in-memory store behind its storage methods. The
    ``emit_<kind>`` methods record their calls and do nothing."""
    session = create_autospec(protocol, instance=True)
    storage = MemoryStorage()
    session.storage = storage
    session.store_raw_data.side_effect = lambda data: storage.store_raw_data_sync(data)
    session.is_known.side_effect = lambda entity_id: storage.is_known(entity_id)
    return session


def make_assignment(**inputs: ArrayTypeSpec) -> WorkflowAssignment:
    return WorkflowAssignment(
        attempt_id=ATTEMPT,
        workflow_name="wf",
        pathname="root/wf",
        cache_key_prefix="cache/wf",
        inputs=frozendict(
            {name: (T(i), ArrayType.of(spec)) for i, (name, spec) in enumerate(inputs.items())}
        ),
    )


def make_context(
    session: Any, **inputs: ArrayTypeSpec
) -> tuple[RunnerWorkflowContext, dict[str, RunnerArrayHandle[Any]]]:
    """A context for an assignment with the given inputs, plus handles to its input nodes built
    the way the worker builds them."""
    assignment = make_assignment(**inputs)
    session.assignment = assignment
    context = RunnerWorkflowContext(session)
    handles = {
        name: RunnerArrayHandle[Any](context, token, type)
        for name, (token, type) in assignment.inputs.items()
    }
    return context, handles


def emitted(session: Any) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    """The ``emit_<kind>`` calls made on the run, in order: the method name, its positional
    arguments (the new node's token first), and its keyword arguments."""
    result: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    for call in session.mock_calls:
        if call[0].startswith("emit_"):
            result.append((call[0], call.args, dict(call.kwargs)))
    return result


def emitted_queries(session: Any) -> list[tuple[NodeToken, tuple[NodeToken, ...], QueryPlan]]:
    """The query nodes emitted for the attempt, in order: each one's token, its input tokens, and
    its decoded plan."""
    return [
        (args[0], tuple(args[1]), decode_query(args[2]))
        for kind, args, _ in emitted(session)
        if kind == "emit_query"
    ]


# ---- workflow context ----


def test_context_exposes_assignment_and_input_handles() -> None:
    session = make_session()
    context, handles = make_context(session, prompts=("[prompt: int]", str), n=int)
    assert context.pathname == "root/wf"
    assert context.default_cache_address_prefix == "cache/wf"
    prompts = handles["prompts"]
    assert node_token_of(context, prompts) == IN0
    assert prompts.type == PROMPTS
    assert prompts.dims == PROMPTS.dims and prompts.schema == PROMPTS.schema
    assert emitted(session) == []


async def test_add_source_array_emits_and_names_are_unique() -> None:
    session = make_session()
    context, _ = make_context(session)
    array = Array.from_items([((1,), "a")], PROMPTS)
    handle = context.add_source_array("prompts", array)
    assert handle.type == PROMPTS
    # expect_type asserts a handle's type inline, while the workflow describes its DAG.
    assert handle.expect_type("[prompt: int]") is handle
    # With a full schema given, the handle is retyped to it (same node, adopting its presentation).
    retyped = handle.expect_type(PROMPTS)
    assert retyped.type == PROMPTS
    assert node_token_of(context, retyped) == node_token_of(context, handle)
    with pytest.raises(ArrayTypeError, match="ArrayHandle: expected"):
        handle.expect_type("[sample: int]")
    # The array itself goes to the host with the description, under the first free token (the
    # assignment has no inputs); the handle carries that token.
    assert emitted(session) == [("emit_source_array", (T(0), "prompts", array), {})]
    assert node_token_of(context, handle) == T(0)
    combined = context.combine({"p": handle})
    assert emitted(session)[-1] == ("emit_combine", (T(1), {"p": T(0)}, "exact"), {})
    assert combined.type == ArrayType.of(("[prompt: int]", struct_of(p=str)))
    with pytest.raises(DuplicateNodeNameError):
        context.add_source_array("prompts", array)
    # Names are shared across source arrays, steps and child workflows.
    samples = context.add_source_array("s", Array.from_items([((1,), "a")], ("[sample: int]", str)))
    with pytest.raises(DuplicateNodeNameError):
        context.run_step("prompts", count, {"samples": samples})


async def test_source_arrays_are_sent_presentation_free() -> None:
    class Point(TypedDict):
        x: int

    session = make_session()
    context, _ = make_context(session)
    array = Array.from_items([((1,), {"x": 1})], ("[k: int]", Point))
    assert needs_presentation(array.type.schema)
    handle = context.add_source_array("t", array)
    ((_, (_, _, sent), _),) = emitted(session)
    assert sent.type == array.type.strip_presentation()
    assert not needs_presentation(sent.type.schema)
    assert handle.type == array.type  # the handle keeps the presented type


async def test_observe_forwards_to_session() -> None:
    session = make_session()
    array = Array.from_items([((1,), "a")], PROMPTS)
    session.observe.return_value = array
    context, handles = make_context(session, prompts=("[prompt: int]", str), x=int)
    handle = handles["prompts"]
    assert handle.type == PROMPTS
    assert node_token_of(context, handle) == IN0
    assert await handle.observe() == array  # retyped to the handle's type, so a new object
    session.observe.assert_awaited_once_with(IN0)
    session.observe.return_value = Array.scalar(1, int)
    await handles["x"].observe()
    session.observe.assert_awaited_with(T(1))
    assert emitted(session) == []


def test_run_step_computes_mapped_type_and_cache_key_prefix() -> None:
    session = make_session()
    context, handles = make_context(session, prompts=("[prompt: int]", str), n=int)
    prompts, n = handles["prompts"], handles["n"]
    samples = context.run_step(
        "sample", sample, {"prompt": prompts, "n": n}, map_over=["prompt"], join="inner"
    )
    assert samples.type == ArrayType.of(("[prompt: int, sample: int]", str))
    counts = context.run_step(
        "count",
        count,
        {"samples": samples},
        map_over=["prompt"],
        override_cache_address_prefix="elsewhere/count",
    )
    assert counts.type == ArrayType.of(("[prompt: int]", int))
    assert emitted(session) == [
        (
            "emit_run_step",
            (T(2),),
            dict(
                name="sample",
                step_name=sample.name,
                inputs={"prompt": IN0, "n": IN1},
                map_over=("prompt",),
                join="inner",
                replicas=frozendict(),
                cache_key_prefix="cache/wf/sample",
                type=ArrayType.of(("[prompt: int, sample: int]", str)),
            ),
        ),
        (
            "emit_run_step",
            (T(3),),
            dict(
                name="count",
                step_name=count.name,
                inputs={"samples": T(2)},
                map_over=("prompt",),
                join="exact",
                replicas=frozendict(),
                cache_key_prefix="elsewhere/count",
                type=ArrayType.of(("[prompt: int]", int)),
            ),
        ),
    ]
    assert node_token_of(context, samples) == T(2) and node_token_of(context, counts) == T(3)


def test_run_step_and_child_workflow_carry_replicas() -> None:
    session = make_session()
    context, handles = make_context(session, prompts=("[prompt: int]", str), n=int)
    prompts, n = handles["prompts"], handles["n"]
    samples = context.run_step(
        "sample", sample, {"prompt": prompts, "n": n}, map_over=["prompt"], replicas={"rep": [0, 1]}
    )
    assert samples.type == ArrayType.of(("[prompt: int, rep: int, sample: int]", str))
    scores = context.run_child_workflow(
        "score", score_prompt, {"prompt": prompts}, map_over=["prompt"], replicas={"try": ["a"]}
    )
    assert scores.type.dim_names == ("prompt", "try", *SCORES.dim_names[1:])
    (run_name, _, run), (child_name, _, child) = emitted(session)
    assert run_name == "emit_run_step" and run["replicas"] == {"rep": (0, 1)}
    assert child_name == "emit_run_child_workflow" and child["replicas"] == {"try": ("a",)}
    with pytest.raises(SignatureError, match="dims of the inputs"):
        context.run_step("again", sample, {"prompt": prompts, "n": n}, replicas={"prompt": [0]})


def test_run_step_signature_errors_surface_before_emitting() -> None:
    session = make_session()
    context, handles = make_context(session, prompts=("[prompt: int]", str), n=int)
    with pytest.raises(SignatureError):
        context.run_step("sample", sample, {"prompt": handles["prompts"]})
    assert emitted(session) == []
    # The name is not consumed by a failed call.
    prompts, n = handles["prompts"], handles["n"]
    context.run_step("sample", sample, {"prompt": prompts, "n": n}, map_over=["prompt"])
    assert len(emitted(session)) == 1


def test_run_child_workflow_emits_node() -> None:
    session = make_session()
    context, handles = make_context(session, prompts=("[prompt: int]", str))
    result = context.run_child_workflow(
        "score", score_prompt, {"prompt": handles["prompts"]}, map_over=["prompt"]
    )
    assert result.type == SCORES
    assert emitted(session) == [
        (
            "emit_run_child_workflow",
            (T(1),),
            dict(
                name="score",
                workflow_name=score_prompt.name,
                inputs={"prompt": IN0},
                map_over=("prompt",),
                join="exact",
                replicas=frozendict(),
                cache_key_prefix="cache/wf/score",
                type=SCORES,
            ),
        )
    ]


def test_combine_and_stack_emit_nodes_with_types() -> None:
    session = make_session()
    context, handles = make_context(session, a=("[k: int]", int), b=str)
    a, b = handles["a"], handles["b"]
    both = context.combine({"a": a, "b": b}, join="outer")
    assert both.type == ArrayType.of(("[k: int]", struct_of(a=optional(int), b=optional(str))))
    stacked = context.stack({"y": a, "x": a}, dim="which")
    assert stacked.type == ArrayType.of(("[which: str, k: int]", int))
    # Stack inputs go in the order given; canonical ordering is the host's.
    assert emitted(session) == [
        ("emit_combine", (T(2), {"a": IN0, "b": IN1}, "outer"), {}),
        ("emit_stack", (T(3), {"y": IN0, "x": IN0}, "which"), {}),
    ]
    with pytest.raises(ArrayTypeError, match="all be int or all be str"):
        context.stack({1: a, "y": a}, dim="which")
    with pytest.raises(ArrayTypeError):
        context.stack({"x": a, "y": b}, dim="which")
    assert len(emitted(session)) == 2  # type errors surface before anything is emitted


def test_handle_field_sel_and_rename_start_queries_recorded_when_collected() -> None:
    session = make_session()
    t_type = ArrayType.of(("[k: int, j: str]", struct_of(x=int, y=str)))
    context, handles = make_context(session, t=t_type)
    t = handles["t"]
    x = t.field("x")
    assert x.type == ArrayType.of(("[k: int, j: str]", int))
    row = t.sel({"k": 3})
    assert row.type == ArrayType.of(("[j: str]", struct_of(x=int, y=str)))
    renamed = t.rename({"k": "kk"})
    assert renamed.type == ArrayType.of(("[kk: int, j: str]", struct_of(x=int, y=str)))
    assert emitted(session) == []  # building a query records nothing
    for query in (x, row, renamed):
        node_token_of(context, query.collect())
    assert emitted_queries(session) == [
        (T(1), (IN0,), over(IN0, t_type).field("x").plan),
        (T(2), (IN0,), over(IN0, t_type).sel({"k": 3}).plan),
        (T(3), (IN0,), over(IN0, t_type).rename({"k": "kk"}).plan),
    ]
    with pytest.raises(ArrayTypeError):
        t.field("nope")
    with pytest.raises(ArrayTypeError):
        t.sel({"zzz": 1})


# ---- workflow queries ----

FLOATS = ArrayType.of(("[k: int]", float))


def over(token: NodeToken, type: ArrayType = FLOATS) -> PlannedQuery[NodeToken]:
    """The expected plan's starting point: ``token``'s node read unchanged."""
    return PlannedQuery.over(token, type)


def test_a_query_is_recorded_as_one_node_only_when_it_is_collected() -> None:
    session = make_session()
    context, handles = make_context(session, s=FLOATS)
    doubled = handles["s"].query().filter(value() >= 1).select(value() * 2)
    assert doubled.type == FLOATS
    assert emitted(session) == []  # building a query records nothing
    assert node_token_of(context, doubled.collect()) == T(1)
    expected = over(IN0).filter(value() >= 1).select(value() * 2)
    assert emitted_queries(session) == [(T(1), (IN0,), expected.plan)]
    # Collecting again records it again, under a token of its own; the host knows the two
    # descriptions as one node, since equal plans over equal inputs are.
    assert node_token_of(context, doubled.collect()) == T(2)
    assert emitted_queries(session)[1] == (T(2), (IN0,), expected.plan)


def test_queries_built_on_one_query_each_record_their_whole_chain() -> None:
    """What is recorded depends only on what was collected, never on what else was built, so
    it is the same however the body's tasks interleave."""
    session = make_session()
    context, handles = make_context(session, s=FLOATS)
    kept = handles["s"].query().filter(value() >= 1)
    doubled = kept.select(value() * 2).collect()
    tripled = kept.select(value() * 3).collect()
    context.combine({"doubled": doubled, "tripled": tripled})
    assert emitted_queries(session) == [
        (T(1), (IN0,), over(IN0).filter(value() >= 1).select(value() * 2).plan),
        (T(2), (IN0,), over(IN0).filter(value() >= 1).select(value() * 3).plan),
    ]


def test_a_collected_query_is_a_node_later_queries_read() -> None:
    session = make_session()
    context, handles = make_context(session, s=FLOATS)
    kept = handles["s"].query().filter(value() >= 1).collect()
    kept.query().select(value() * 2).collect()
    kept.query().select(value() * 3).collect()
    assert emitted_queries(session) == [
        (T(1), (IN0,), over(IN0).filter(value() >= 1).plan),
        (T(2), (T(1),), over(T(1)).select(value() * 2).plan),
        (T(3), (T(1),), over(T(1)).select(value() * 3).plan),
    ]


def test_a_handle_query_shortcut_starts_the_same_query_as_query() -> None:
    session = make_session()
    context, handles = make_context(session, s=FLOATS)
    handles["s"].filter(value() >= 1).collect()
    handles["s"].aggregate({"mean": value().mean()}).collect()
    assert emitted_queries(session) == [
        (T(1), (IN0,), over(IN0).filter(value() >= 1).plan),
        (T(2), (IN0,), over(IN0).aggregate({"mean": value().mean()}).plan),
    ]


def test_a_query_never_collected_records_nothing() -> None:
    session = make_session()
    _, handles = make_context(session, s=FLOATS)
    handles["s"].query().filter(value() >= 1).select(value() * 2)
    assert emitted(session) == []


def test_sql_reading_one_query_twice_is_one_node_over_both_reads() -> None:
    session = make_session()
    context, handles = make_context(session, s=FLOATS)
    kept = handles["s"].query().filter(value() >= 1)
    text = "SELECT a.dims, a.value FROM a JOIN b USING (dims)"
    joined = fxtr.sql(text, tables={"a": kept, "b": kept}, output_type=FLOATS)
    assert emitted(session) == []
    node_token_of(context, joined.collect())
    read = over(IN0).filter(value() >= 1)
    expected = PlannedQuery.sql(text, tables={"a": read, "b": read}, output_type=FLOATS)
    assert emitted_queries(session) == [(T(1), (IN0, IN0), expected.plan)]


def test_field_sel_and_rename_on_a_query_are_part_of_its_node() -> None:
    session = make_session()
    t_type = ArrayType.of(("[k: int, j: str]", struct_of(x=int, y=str)))
    context, handles = make_context(session, t=t_type)
    query = handles["t"].query().filter(dim("k") > 0)
    sliced = query.sel({"j": "a"}).rename({"k": "kk"}).field("x")
    assert sliced.type == ArrayType.of(("[kk: int]", int))
    node_token_of(context, sliced.collect())
    expected = over(IN0, t_type).filter(dim("k") > 0).sel({"j": "a"}).rename({"k": "kk"})
    assert emitted_queries(session) == [(T(1), (IN0,), expected.field("x").plan)]
    assert len(emitted(session)) == 1


def test_query_errors_surface_at_the_call_and_record_nothing() -> None:
    session = make_session()
    _, handles = make_context(session, t=("[k: int]", struct_of(x=int)))
    kept = handles["t"].query().filter(dim("k") > 0)
    with pytest.raises(ArrayTypeError):
        kept.field("nope")
    with pytest.raises(ArrayTypeError):
        kept.sel({"zzz": 1})
    assert emitted(session) == []


def test_a_query_keeps_the_fields_it_was_given_when_the_caller_changes_them() -> None:
    session = make_session()
    t_type = ArrayType.of(("[k: int]", struct_of(x=int, y=str)))
    context, handles = make_context(session, t=t_type)
    fields = {"x": value().field("x")}
    selected = handles["t"].query().select(fields)
    fields["y"] = value().field("y")
    assert selected.type == ArrayType.of(("[k: int]", struct_of(x=int)))
    node_token_of(context, selected.collect())
    expected = over(IN0, t_type).select({"x": value().field("x")})
    assert emitted_queries(session) == [(T(1), (IN0,), expected.plan)]


def test_a_retype_that_changes_the_stored_type_records_a_query_node_at_once() -> None:
    session = make_session()
    m_type = ArrayType.of(("[row: int, column: str]", float))
    reordered_type = ArrayType.of(("[column: str, row: int]", float))
    context, handles = make_context(session, m=m_type)
    reordered = handles["m"].retype(reordered_type)
    assert node_token_of(context, reordered) == T(1)
    # A query after the retype reads the node in its stored order.
    reordered.query().filter(dim("row") > 0).collect()
    assert emitted_queries(session) == [
        (T(1), (IN0,), over(IN0, m_type).retype(reordered_type).plan),
        (T(2), (T(1),), over(T(1), reordered_type).filter(dim("row") > 0).plan),
    ]


def test_a_retype_that_changes_only_presentation_names_the_same_node() -> None:
    class Point(TypedDict):
        x: int

    session = make_session()
    context, handles = make_context(session, p=("[k: int]", struct_of(x=int)))
    presented = handles["p"].retype(("[k: int]", Point))
    assert needs_presentation(presented.type.schema)
    assert node_token_of(context, presented) == IN0
    # So does a query that only reads a handle and presents it differently.
    assert node_token_of(context, handles["p"].query().retype(("[k: int]", Point)).collect()) == IN0
    assert emitted(session) == []


def test_a_query_not_collected_is_refused_where_a_node_is_needed() -> None:
    session = make_session()
    context, handles = make_context(session, s=FLOATS)
    query = cast("Any", handles["s"].query().filter(value() >= 1))
    with pytest.raises(TypeError, match=r"call \.collect\(\)"):
        node_token_of(context, query)
    with pytest.raises(TypeError, match=r"call \.collect\(\)"):
        context.combine({"kept": query})
    assert emitted(session) == []


def test_a_query_over_another_attempts_handle_is_refused_when_collected() -> None:
    session = make_session()
    mine, handles = make_context(session, s=FLOATS)
    _, theirs = make_context(session, s=FLOATS)
    joined = handles["s"].query().join(theirs["s"], on=[(dim("k"), dim("k"))])
    with pytest.raises(ForeignArrayHandleError):
        joined.collect()
    assert emitted(session) == []


def test_foreign_handles_are_rejected() -> None:
    session = make_session()
    mine, _ = make_context(session, a=int)
    other, theirs = make_context(session, a=int)
    with pytest.raises(ForeignArrayHandleError):
        mine.combine({"a": theirs["a"]})
    with pytest.raises(ForeignArrayHandleError):
        node_token_of(mine, theirs["a"])
    assert emitted(session) == []


async def test_workflow_context_storage_forwards_to_the_session() -> None:
    session = make_session()
    session.store_raw_data.side_effect = None
    session.store_raw_data.return_value = FAKE_ID
    session.is_known.side_effect = None
    session.is_known.return_value = True
    session.load_raw_data.return_value = {"$type": "fxtr.builtin.symbol_type"}
    context, _ = make_context(session)
    assert await context.store_raw_data({"$type": "x"}) == FAKE_ID
    session.store_raw_data.assert_called_once_with({"$type": "x"})
    assert context.is_known(FAKE_ID) is True
    session.is_known.assert_called_once_with(FAKE_ID)
    assert await context.load_raw_data(FAKE_ID) == {"$type": "fxtr.builtin.symbol_type"}
    session.load_raw_data.assert_awaited_once_with(FAKE_ID)


# ---- step context ----


async def test_step_context_exposes_cache_key_and_forwards_storage() -> None:
    session = make_session(StepRunSession)
    session.store_raw_data.side_effect = None
    session.store_raw_data.return_value = FAKE_ID
    assignment = StepAssignment(
        attempt_id=ATTEMPT,
        step_name=count.name,
        cache_key="cache/wf/count[prompt:1]",
        inputs=frozendict(),
    )
    session.assignment = assignment
    context = RunnerStepContext(session)
    assert context.cache_address == "cache/wf/count[prompt:1]"
    assert await context.store_raw_data({"$type": "x"}) == FAKE_ID
    session.store_raw_data.assert_called_once_with({"$type": "x"})
    await context.load_raw_data(FAKE_ID)
    session.load_raw_data.assert_awaited_once_with(FAKE_ID)


# ---- logging ----


async def test_workflow_context_log_forwards_to_session() -> None:
    session = make_session()
    context, _ = make_context(session)
    context.log("hello from the body")
    session.log_message.assert_called_once_with("hello from the body")


async def test_step_context_log_forwards_to_session() -> None:
    session = make_session(StepRunSession)
    assignment = StepAssignment(
        attempt_id=ATTEMPT,
        step_name=count.name,
        cache_key="cache/wf/count[prompt:1]",
        inputs=frozendict(),
    )
    session.assignment = assignment
    RunnerStepContext(session).log("step says hi")
    session.log_message.assert_called_once_with("step says hi")
