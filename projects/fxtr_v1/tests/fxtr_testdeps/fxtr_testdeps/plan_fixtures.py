"""Plan fixtures for tests."""

import abc
import functools
from typing import Annotated, Any, Collection

from frozendict import frozendict

from fxtr.impl_plans import plan_helpers
from fxtr.object_model import core


class FooInterface[T, U: core.AnyEntity | str](
    core.FxtrCallTarget,
    interface_id="FooInterface^1",
    namespace_id="!playground",
    stateless=True,
):
    """Test interface"""

    @abc.abstractmethod
    async def bar(self, arg: T) -> U:
        """Test method"""


def make_bad_foo_impl_with_uninferable_interface_spec():
    class BadDynamicFooImpl[Z: core.AnyEntity | str](FooInterface[int, Z]):
        """Bad implementaiton of FooInterface (cannot infer interface spec)"""

        interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS  # <- doesn't work due to Z

        def __init__(self, z: Z):
            self._z = z

        async def bar(self, arg: int) -> Z:
            return self._z

    return BadDynamicFooImpl


class DynamicFooImpl[Z: core.AnyEntity | str](FooInterface[int, Z]):
    """Test interface"""

    def __init__(self, tyZ: type[Z], z: Z):
        self._tyZ = tyZ
        self._z = z

    def interface_spec(self) -> core.InterfaceSpec[FooInterface[int, Z]]:
        return core.spec_of(FooInterface[int, self._tyZ])

    async def bar(self, arg: int) -> Z:
        assert isinstance(arg, int)
        return self._z


def statically_make_dynamic_foo_impl(resolver: core.PlanResolver, z: str) -> DynamicFooImpl[str]:
    del resolver
    return DynamicFooImpl(str, z)


def dynamically_make_dynamic_foo_impl[
    X: core.AnyEntity | str
](resolver: core.PlanResolver, z: X) -> DynamicFooImpl[X]:
    del resolver
    return DynamicFooImpl(type(z), z)


class StaticFooImpl(FooInterface[int, str]):
    """Static implementation of FooInterface"""

    def __init__(self, resolver: core.PlanResolver, other: str) -> None:
        self._resolver = resolver
        self._other = other

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    async def bar(self, arg: int) -> str:
        assert isinstance(arg, int)
        return self._other


@functools.cache
def mk_external_plan(name: str) -> core.Plan[core.StorageHandle[core.Run]]:
    return core.spec_of(core.StorageHandle[core.Run]).define_external_service_impl(
        service_id=f"external_plan_impl_{name}^1",
        doc=f"External plan implementation {name}",
        namespace_id="!playground",
    )


class MutableIncrementerInterface(
    core.FxtrCallTarget,
    interface_id="MutableIncrementerInterface^1",
    namespace_id="!playground",
    stateless=False,
):
    """Test mutable interface"""

    @abc.abstractmethod
    async def increment(self, by: int) -> int:
        """Increment the counter by the given amount"""


class MutableIncrementerImpl(MutableIncrementerInterface):
    """Test mutable implementation"""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(self, resolver: core.PlanResolver | None = None):
        self._counter = 0

    async def increment(self, by: int) -> int:
        self._counter += by
        return self._counter


def _extract_int(value: int) -> Collection[core.Plan[Any]]:
    assert value == 42
    return [mk_external_plan("dynamic_a"), mk_external_plan("dynamic_b")]


def make_with_inferred_runtime_deps(
    resolver: core.PlanResolver,
    runtime_plan_1: plan_helpers.RuntimeDepPlan[core.StorageHandle[core.Run]],
    not_runtime_plan: core.Plan[core.StorageHandle[core.Run]],
    tuple_of_dict_of_plans: tuple[
        frozendict[
            str,
            tuple[plan_helpers.RuntimeDepPlan[core.StorageHandle[core.Run]] | None, ...],
        ],
        core.Plan[core.StorageHandle[core.Run]],
    ],
    extract_plan_dynamically: Annotated[int, plan_helpers.DependencyAtRuntime(_extract_int)],
    *,
    _default_runtime_plan: plan_helpers.RuntimeDepPlan[
        core.StorageHandle[core.Run]
    ] = mk_external_plan("_default_runtime_plan"),
    _default_other_value: core.Plan[core.StorageHandle[core.Run]] = mk_external_plan(
        "_default_other_value"
    ),
    **kwargs: plan_helpers.RuntimeDepPlan[core.StorageHandle[core.Run]],
) -> DynamicFooImpl[str]:
    return DynamicFooImpl(str, "what")


type SomeGeneric[T] = Annotated[
    Collection[Annotated[int, plan_helpers.DependencyAtRuntime(_extract_int)]], "foo"
]


def make_with_unextractable_runtime_dep_ann(
    resolver: core.PlanResolver,
    unextractable_runtime_dep_ann: tuple[SomeGeneric[str], ...],
) -> DynamicFooImpl[str]:
    return DynamicFooImpl(str, "what")


@plan_helpers.task_function
async def my_test_task_function(
    resolver: core.PlanResolver,
    x: int,
    dep: plan_helpers.RuntimeDepPlan[core.StorageHandle[core.Run]],
) -> core.AdHocMetadata:
    _unused_dep_impl = resolver(dep)
    return core.AdHocMetadata(metadata=core.freeze({"x": x}))


@plan_helpers.task_function
async def my_generic_task_function[
    T: core.AnyEntity
](resolver: core.PlanResolver, x: T,) -> T:
    return x


class Wrapper[T: core.AnyEntity](
    core.DataEntityBase, type_id="Wrapper^1", namespace_id="!playground"
):
    foo: T


@plan_helpers.task_function
async def my_parameterized_retval_task_function[
    T: core.AnyEntity
](resolver: core.PlanResolver, x: Wrapper[T],) -> Wrapper[T]:
    return x


class MyProtocolWithGenerics[T: core.AnyEntity](
    core.FxtrCallTarget,
    interface_id="MyProtocolWithGenerics^1",
    namespace_id="!playground",
    stateless=True,
):
    @abc.abstractmethod
    async def foo(self, x: T, wrapper: Wrapper[T]) -> T:
        """A test method that has type vars in its args and return type."""


def make_protocol_with_generic_method_type():

    class MyProtocolWithGenericCallTargetType[T: core.AnyEntity](
        core.FxtrCallTarget,
        interface_id="MyProtocolWithGenericMethodType^1",
        namespace_id="!playground",
        stateless=True,
    ):
        @abc.abstractmethod
        async def my_method(self, x: T) -> MyProtocolWithGenerics[T]:
            """A test method that has a parameterized call target type in its return type."""
            ...

    return MyProtocolWithGenericCallTargetType


class WrappedInt(core.DataEntityBase, type_id="WrappedInt^1", namespace_id="!playground"):
    """Test entity for wrapping an integer value."""

    value: int


def add_delta_untasked(input: WrappedInt, delta: int) -> WrappedInt:
    """Sync function that adds delta to input value."""
    return WrappedInt(value=input.value + delta)


async def add_stuff_untasked(
    val1: WrappedInt,
    val2handle: core.StorageHandle[WrappedInt],
    delta: int,
) -> WrappedInt:
    """Async function that adds val1, val2 (from handle), and delta."""
    val2 = await val2handle.retrieve()
    return WrappedInt(value=val1.value + val2.value + delta)


@plan_helpers.task_function
async def add_one(
    resolver: core.PlanResolver,
    input_node: plan_helpers.RuntimeDepPlan[core.StorageHandle[WrappedInt]],
) -> WrappedInt:
    """Task function that adds 1 to the value in the input node."""
    val = await resolver(input_node).retrieve()
    return WrappedInt(value=val.value + 1)


@plan_helpers.workflow_function
async def repeatedly_add(
    resolver: core.PlanResolver,
    input_node: plan_helpers.RuntimeDepPlan[core.StorageHandle[WrappedInt]],
    num_times: int,
) -> core.WorkflowState[int, WrappedInt] | core.WorkflowResult[WrappedInt]:
    """Workflow function that repeatedly adds 1 to the input value num_times."""
    if num_times == 0:
        value = await resolver(input_node).retrieve()
        return core.WorkflowResult(result=value)
    else:
        add_task = add_one.bind(input_node=input_node)
        return core.WorkflowState(
            step_data=core.freeze({"subtask": add_task}),
            next=repeatedly_add.bind(input_node=add_task.result_handle, num_times=num_times - 1),
        )


class UnresolvingFooDoubler(FooInterface[int, str]):
    """Resolver-free implementation of FooInterface that doubles the result of another FooInterface."""

    def __init__(self, other: FooInterface[int, str]) -> None:
        self._other = other

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    async def bar(self, arg: int) -> str:
        v = await self._other.bar(arg)
        return v + v
