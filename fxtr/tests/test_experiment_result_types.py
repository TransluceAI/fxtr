"""The static types of a step's or workflow's results: what the decorators' overloads work out.

``_static_checks`` is never called; pyright checks its ``assert_type`` calls, which pin down the
``R`` (body result) and ``V`` (produced value) type parameters of ``Step`` and ``Workflow`` and so
the handle types ``run_step`` and ``run_child_workflow`` return. The test below only checks that
the same bodies decorate at runtime.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any, TypedDict, assert_type

from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array, ScalarArray
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.experiment.handles import ArrayHandle, ScalarHandle
from fxtr.experiment.steps import Step, StepContext, step
from fxtr.experiment.workflows import Workflow, WorkflowContext, workflow


@dataclasses.dataclass(frozen=True, kw_only=True)
class Config(DataclassEntity, fxtr_type="test.fxtr.result_types.config"):
    model: str


class Made(TypedDict):
    config: BoundID[Config] | None
    note: str


# ---- steps ----


@step()
async def entity(context: StepContext, m: str) -> Config:
    return Config(model=m)


@step()
async def optional_entity(context: StepContext, m: str) -> Config | None:
    return None


@step()
async def bound(context: StepContext, m: str) -> BoundID[Config]:
    return await context.store(Config(model=m))


@step()
async def struct(context: StepContext, m: str) -> Made:
    return {"config": None, "note": m}


@step()
async def leaf(context: StepContext, m: str) -> str:
    return m


@step()
async def leaf_array(context: StepContext, m: str) -> Annotated[Array[str], "[i: int]"]:
    return Array.from_items([((0,), m)], ("[i: int]", str))


@step()
async def entity_array(context: StepContext, m: str) -> Annotated[Array[Config], "[i: int]"]:
    raise NotImplementedError


@step()
async def bound_scalar_array(context: StepContext, m: str) -> ScalarArray[BoundID[Config]]:
    raise NotImplementedError


# ---- workflows ----


@workflow()
async def wf_entity(context: WorkflowContext, m: str) -> Config:
    return Config(model=m)


@workflow()
async def wf_optional_entity(context: WorkflowContext, m: str) -> Config | None:
    return None


@workflow()
async def wf_leaf(context: WorkflowContext, m: str) -> str:
    return m


@workflow()
async def wf_entity_handle(context: WorkflowContext, m: str) -> ScalarHandle[Config]:
    raise NotImplementedError


@workflow()
async def wf_bound_handle(
    context: WorkflowContext, m: str
) -> Annotated[ArrayHandle[BoundID[Config]], "[i: int]"]:
    raise NotImplementedError


@workflow()
async def wf_struct_handle(context: WorkflowContext, m: str) -> ScalarHandle[Made]:
    raise NotImplementedError


def step_types[R, V](s: Step[Any, R, V]) -> tuple[R, V]: ...


def workflow_types[R, V](w: Workflow[Any, R, V]) -> tuple[R, V]: ...


def _static_checks(context: WorkflowContext, ms: Annotated[ArrayHandle[str], "[j: int]"]) -> None:
    # (R, V) of the decorated objects: what the body returns, and what the step produces.
    assert_type(step_types(entity), tuple[Config, BoundID[Config]])
    assert_type(step_types(optional_entity), tuple[Config | None, BoundID[Config] | None])
    assert_type(step_types(bound), tuple[BoundID[Config], BoundID[Config]])
    assert_type(step_types(struct), tuple[Made, Made])
    assert_type(step_types(leaf), tuple[str, str])
    assert_type(step_types(leaf_array), tuple[str, str])
    assert_type(step_types(entity_array), tuple[Config, BoundID[Config]])
    assert_type(step_types(bound_scalar_array), tuple[BoundID[Config], BoundID[Config]])
    assert_type(workflow_types(wf_entity), tuple[Config, BoundID[Config]])
    assert_type(workflow_types(wf_optional_entity), tuple[Config | None, BoundID[Config] | None])
    assert_type(workflow_types(wf_leaf), tuple[str, str])
    assert_type(workflow_types(wf_entity_handle), tuple[Config, BoundID[Config]])
    assert_type(workflow_types(wf_bound_handle), tuple[BoundID[Config], BoundID[Config]])
    assert_type(workflow_types(wf_struct_handle), tuple[Made, Made])

    # Scheduling gives a handle to the produced values.
    inputs = {"m": ms}
    assert_type(context.run_step("a", entity, inputs), ArrayHandle[BoundID[Config]])
    assert_type(context.run_step("b", optional_entity, inputs), ArrayHandle[BoundID[Config] | None])
    assert_type(context.run_step("c", struct, inputs), ArrayHandle[Made])
    assert_type(context.run_step("d", leaf_array, inputs), ArrayHandle[str])
    assert_type(context.run_step("e", entity_array, inputs), ArrayHandle[BoundID[Config]])
    assert_type(context.run_child_workflow("f", wf_entity, inputs), ArrayHandle[BoundID[Config]])
    assert_type(
        context.run_child_workflow("g", wf_entity_handle, inputs), ArrayHandle[BoundID[Config]]
    )

    # A step producing references fits wherever one is expected, whichever way it returns them.
    def takes_refs(s: Step[Any, Any, BoundID[Config]]) -> None: ...

    takes_refs(entity)
    takes_refs(bound)
    takes_refs(entity_array)


def test_the_bodies_decorate() -> None:
    assert isinstance(entity, Step) and isinstance(wf_entity, Workflow)
