"""Tests for the function registry: lookup by name and kind, registration from the decorators,
duplicate and ambiguous names, and the naming rule for functions defined in ``__main__``."""

from __future__ import annotations

from typing import Any

import pytest

from fxtr.entity_defns.array import Array, ArrayType
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.registry import (
    GLOBAL_REGISTRY,
    FunctionRegistry,
    RegistryError,
    default_name,
)
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow

SAMPLES = ArrayType.of(("[sample: int]", str))


@step(inputs={"prompt": str, "n": int}, output=SAMPLES)
async def sample(context: StepContext, prompt: str, n: int) -> Array[str]:
    return Array.from_items([((i,), f"{prompt}-{i}") for i in range(n)], SAMPLES)


@step(inputs={"samples": SAMPLES}, output=int)
async def count(context: StepContext, samples: Array[str]) -> int:
    return len(samples)


@workflow(inputs={"prompt": str, "n": int}, output=int)
async def count_samples(
    context: WorkflowContext, prompt: ArrayHandle[str], n: ArrayHandle[int]
) -> ArrayHandle[int]:
    samples = context.run_step("sample", sample, {"prompt": prompt, "n": n})
    return context.run_step("count", count, {"samples": samples})


def test_lookup_by_name_and_kind() -> None:
    registry = FunctionRegistry([sample, count, count_samples])
    assert registry.functions == (sample, count, count_samples)
    assert registry.step(sample.name) is sample
    assert registry.workflow(count_samples.name) is count_samples
    with pytest.raises(RegistryError, match="No function named"):
        registry.step("nowhere.missing")
    with pytest.raises(RegistryError, match="is a workflow"):
        registry.step(count_samples.name)
    with pytest.raises(RegistryError, match="is a step"):
        registry.workflow(sample.name)


def test_adding_the_same_object_again_is_a_no_op() -> None:
    registry = FunctionRegistry([sample])
    registry.add(sample)
    assert registry.functions == (sample,)
    assert registry.step(sample.name) is sample


def test_an_ambiguous_name_fails_only_when_looked_up() -> None:
    @step(name="shared", inputs={"x": int}, output=int)
    async def first(context: StepContext, x: int) -> int:
        return x

    @step(name="shared", inputs={"x": int}, output=int)
    async def second(context: StepContext, x: int) -> int:
        return -x

    registry = FunctionRegistry([first, sample])
    registry.add(second)  # registration never fails
    assert registry.step(sample.name) is sample
    with pytest.raises(RegistryError, match="registered by 2 functions") as info:
        registry.step("shared")
    # The message names where each came from.
    assert f"{__name__}.{first.body.__qualname__}" in str(info.value)
    assert f"{__name__}.{second.body.__qualname__}" in str(info.value)
    with pytest.raises(RegistryError, match="registered by 2 functions"):
        registry.functions
    assert registry.unambiguous() == {sample.name: sample}


def test_the_decorators_register_globally() -> None:
    assert GLOBAL_REGISTRY.step(sample.name) is sample
    assert GLOBAL_REGISTRY.workflow(count_samples.name) is count_samples


def test_names_default_to_the_qualified_name() -> None:
    assert sample.name == f"{__name__}.sample"
    assert count_samples.name == f"{__name__}.count_samples"


def test_a_function_defined_in_main_needs_an_explicit_name() -> None:
    async def body(context: StepContext, x: int) -> int:
        return x

    body.__module__ = "__main__"
    with pytest.raises(TypeError, match="defined in __main__"):
        default_name(body)
    with pytest.raises(TypeError, match="defined in __main__"):
        step(inputs={"x": int}, output=int)(body)
    named: Any = step(name="test_registry.explicitly_named", inputs={"x": int}, output=int)(body)
    assert named.name == "test_registry.explicitly_named"
    assert GLOBAL_REGISTRY.step("test_registry.explicitly_named") is named
