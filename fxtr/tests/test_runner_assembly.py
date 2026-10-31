"""Tests for the host-side assembler: the translation from the worker's token-based descriptions
to nodes, and the token rules."""

from __future__ import annotations

from typing import Any

import pytest
from frozendict import frozendict

from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.query_plan import PlannedQuery
from fxtr.runner import nodes
from fxtr.runner.assembly import NodeAssembler
from fxtr.runner.query_codec import encode_query
from fxtr.runner.session import AttemptID, NodeToken, NodeTokenError

ATTEMPT = AttemptID("attempt-1")
PROMPTS = ArrayType.of(("[prompt: int]", str))
COUNTS = ArrayType.of(("[prompt: int]", int))

RENAMED = encode_query(PlannedQuery.over(0, PROMPTS).rename({"prompt": "p"}).plan)
"""A one-step query plan, as a query node carries it."""


def identity(n: int) -> EntityID:
    """A stand-in identity for a node the host already has."""
    return EntityID(cid=b"\x01\x71\x12\x20" + (n + 1).to_bytes(32, "big"))


def T(n: int) -> NodeToken:
    return NodeToken(n)


def seeded(**inputs: tuple[EntityID, ArrayType]) -> NodeAssembler:
    assembler = NodeAssembler()
    assembler.seed_assignment(
        ATTEMPT, workflow_name="wf", pathname="root/wf", cache_key_prefix="cache/wf", inputs=inputs
    )
    return assembler


def test_seed_assignment_numbers_the_inputs_from_zero() -> None:
    assembler = NodeAssembler()
    assignment = assembler.seed_assignment(
        ATTEMPT,
        workflow_name="wf",
        pathname="root/wf",
        cache_key_prefix="cache/wf",
        inputs={"prompts": (identity(0), PROMPTS), "n": (identity(1), COUNTS)},
    )
    assert assignment.attempt_id == ATTEMPT
    assert (assignment.workflow_name, assignment.pathname) == ("wf", "root/wf")
    assert assignment.cache_key_prefix == "cache/wf"
    assert assignment.inputs == {"prompts": (T(0), PROMPTS), "n": (T(1), COUNTS)}
    assert assembler.resolve(T(0)) == identity(0)
    assert assembler.resolve(T(1)) == identity(1)


def test_tokens_bind_once_and_resolve_only_when_bound() -> None:
    assembler = seeded(a=(identity(0), COUNTS))
    with pytest.raises(NodeTokenError, match="already bound"):
        assembler.bind(T(0), identity(5))
    with pytest.raises(NodeTokenError, match="not bound"):
        assembler.resolve(T(7))
    assembler.bind(T(7), identity(5))
    assert assembler.resolve(T(7)) == identity(5)
    # A builder given a bound token binds nothing and builds nothing.
    with pytest.raises(NodeTokenError, match="already bound"):
        assembler.query(T(7), [T(0)], RENAMED)
    # A builder given an unbound input token fails before binding the new token, so the new
    # token stays free for a corrected description.
    with pytest.raises(NodeTokenError, match="not bound"):
        assembler.query(T(8), [T(99)], RENAMED)
    assembler.query(T(8), [T(0)], RENAMED)


def test_builders_resolve_inputs_and_bind_the_new_token_to_the_identity() -> None:
    assembler = seeded(a=(identity(0), COUNTS), b=(identity(1), PROMPTS))
    combine = assembler.combine(T(2), {"a": T(0), "b": T(1)}, "outer")
    assert combine == nodes.Combine(
        inputs=frozendict({"a": identity(0), "b": identity(1)}), join="outer"
    )
    assert assembler.resolve(T(2)) == combine.content_id()
    query = assembler.query(T(3), [T(2), T(0)], RENAMED)
    assert query == nodes.Query(inputs=(combine.content_id(), identity(0)), plan=RENAMED)
    assert assembler.resolve(T(3)) == query.content_id()


def test_stack_inputs_are_canonically_ordered_by_key() -> None:
    assembler = seeded(a=(identity(0), COUNTS), b=(identity(1), COUNTS))
    one = assembler.stack(T(2), {"y": T(0), "x": T(1)}, "which")
    other = assembler.stack(T(3), {"x": T(1), "y": T(0)}, "which")
    assert one == other == nodes.Stack(inputs=(("x", identity(1)), ("y", identity(0))), dim="which")
    # Two emissions of one description are two tokens bound to one identity.
    assert assembler.resolve(T(2)) == assembler.resolve(T(3)) == one.content_id()


def test_source_array_node_refers_to_the_array_by_content_id() -> None:
    assembler = seeded()
    array = Array.from_items([((1,), "a")], PROMPTS)
    node = assembler.source_array(T(0), "prompts", array)
    assert node == nodes.source_array("prompts", array)
    assert node.array.entity_id == array.content_id()
    assert assembler.resolve(T(0)) == node.content_id()


def test_run_builders_carry_the_description_through() -> None:
    assembler = seeded(prompts=(identity(0), PROMPTS), n=(identity(1), COUNTS))
    common: dict[str, Any] = dict(
        inputs={"prompts": T(0), "n": T(1)},
        map_over=("prompt",),
        join="inner",
        replicas={"rep": (0, 1)},
        cache_key_prefix="cache/wf/sample",
        type=ArrayType.of(("[prompt: int, rep: int, sample: int]", str)),
    )
    step = assembler.run_step(T(2), name="sample", step_name="steps.sample", **common)
    assert step == nodes.RunStep(
        name="sample",
        step_name="steps.sample",
        inputs=frozendict({"prompts": identity(0), "n": identity(1)}),
        map_over=("prompt",),
        join="inner",
        replicas=frozendict({"rep": (0, 1)}),
        cache_key_prefix="cache/wf/sample",
        type=common["type"],
    )
    child = assembler.run_child_workflow(
        T(3), name="score", workflow_name="workflows.score", **common
    )
    assert isinstance(child, nodes.RunChildWorkflow)
    assert (child.name, child.workflow_name) == ("score", "workflows.score")
    assert child.inputs == {"prompts": identity(0), "n": identity(1)}
    assert assembler.resolve(T(2)) == step.content_id()
    assert assembler.resolve(T(3)) == child.content_id()
