"""Tests for plans."""

from typing import Any

import pytest
from frozendict import frozendict
from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

from fxtr.impl_plans import plan_helpers, py_impl_plan
from fxtr.object_model import core


def test_interface_with_nonabstract_methods():

    with pytest.raises(TypeError, match="Method bar on BadInterface1 is not abstract."):

        class BadInterface1(
            core.FxtrCallTarget,
            interface_id="BadInterface1^1",
            namespace_id="!playground",
            stateless=True,
        ):
            """Test interface"""

            async def bar(self, arg: core.AnyEntity) -> core.AnyEntity:
                """Test method"""
                raise NotImplementedError("Not implemented")


def test_interface_with_nonasync_methods():

    with pytest.raises(TypeError, match="Method bar on BadInterface2 is not async."):

        class BadInterface2(
            core.FxtrCallTarget,
            interface_id="BadInterface2^1",
            namespace_id="!playground",
            stateless=True,
        ):
            """Test interface"""

            def bar(self, arg: core.AnyEntity) -> core.AnyEntity:
                """Test method"""
                return arg


def test_cannot_infer_interface_from_generic_base():

    with pytest.raises(
        TypeError, match="Could not automatically infer interface spec from generic base class"
    ):

        class BadFooImpl[Z: core.AnyEntity](plan_fixtures.FooInterface[int, Z]):
            """Test interface"""

            interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS  # <- type variables, won't work

            async def bar(self, arg: int) -> Z:
                """Test method"""
                raise NotImplementedError("Not implemented")


def _get_plan_fixture_module(use_stringized: bool):
    if use_stringized:
        return plan_fixtures_stringized
    else:
        return plan_fixtures


@pytest.mark.parametrize("use_stringized", [False, True])
def test_interface_defn(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    defn = core.interface_defn_of(plan_fixture_module.FooInterface)
    assert defn.interface_id == "FooInterface^1"
    assert defn.namespace_id == "!playground"
    assert defn.type_vars == ("T", "U")
    assert defn.methods == ("bar",)


@pytest.mark.parametrize("use_stringized", [False, True])
def test_spec_of(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    spec = core.spec_of(plan_fixture_module.FooInterface[int, core.Run])
    assert spec.defn == core.interface_defn_of(plan_fixture_module.FooInterface)

    # note: this is the provisional tuplized format
    assert spec.bindings == frozendict({"T": ("n", "int"), "U": ("n", "Run")})


@pytest.mark.parametrize("use_stringized", [False, True])
def test_uninferable_interface_spec(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    with pytest.raises(
        TypeError, match="Could not automatically infer interface spec from generic base class"
    ):
        plan_fixture_module.make_bad_foo_impl_with_uninferable_interface_spec()


@pytest.mark.parametrize("use_stringized", [False, True])
def test_spec_of_dynamic_concrete_target(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    spec = core.spec_of_concrete_target(plan_fixture_module.DynamicFooImpl[str])
    assert spec.defn == core.interface_defn_of(plan_fixture_module.FooInterface)

    # note: this is the provisional tuplized format
    assert spec.bindings == frozendict({"T": ("n", "int"), "U": ("n", "str")})


@pytest.mark.parametrize("use_stringized", [False, True])
def test_generic_method_type(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)
    plan_fixture_module.make_protocol_with_generic_method_type()


@pytest.mark.parametrize("use_stringized", [False, True])
def test_spec_of_static_concrete_target(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    spec = core.spec_of_concrete_target(plan_fixture_module.StaticFooImpl)
    assert spec.defn == core.interface_defn_of(plan_fixture_module.FooInterface)

    # note: this is the provisional tuplized format
    assert spec.bindings == frozendict({"T": ("n", "int"), "U": ("n", "str")})


@pytest.mark.parametrize("use_stringized", [False, True])
def test_capture_constructor_fn(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    plan = plan_helpers.build_plan(plan_fixture_module.statically_make_dynamic_foo_impl).bind(
        z="hi"
    )
    assert plan.constructor == f"{plan_fixture_module.__name__}:statically_make_dynamic_foo_impl"
    assert plan.requires == "fxtr_testdeps"
    assert plan.interface_spec == core.spec_of(plan_fixture_module.FooInterface[int, str])
    assert plan.impl_deps == frozenset()
    assert plan.kwargs == frozendict({"z": "hi"})


@pytest.mark.parametrize("use_stringized", [False, True])
def test_capture_cls_constructor(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    plan = plan_helpers.build_plan(plan_fixture_module.StaticFooImpl).bind(other="hi")
    assert plan.constructor == f"{plan_fixture_module.__name__}:StaticFooImpl"
    assert plan.requires == "fxtr_testdeps"
    assert plan.interface_spec == core.spec_of(plan_fixture_module.FooInterface[int, str])
    assert plan.impl_deps == frozenset()
    assert plan.kwargs == frozendict({"other": "hi"})


@pytest.mark.parametrize("use_stringized", [False, True])
def test_capture_dynamic_constructor(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    with pytest.raises(
        core.ReifiedTypeTypeVarError, match="Type variable X is not allowed in this context"
    ):
        plan_helpers.build_plan(plan_fixture_module.dynamically_make_dynamic_foo_impl)

    plan = plan_helpers.build_plan(
        plan_fixture_module.dynamically_make_dynamic_foo_impl,
        interface_spec=core.spec_of(plan_fixture_module.FooInterface[int, str]),
    ).bind(z="greetings")
    assert plan.constructor == f"{plan_fixture_module.__name__}:dynamically_make_dynamic_foo_impl"
    assert plan.requires == "fxtr_testdeps"
    assert plan.interface_spec == core.spec_of(plan_fixture_module.FooInterface[int, str])
    assert plan.impl_deps == frozenset()
    assert plan.kwargs == frozendict({"z": "greetings"})


@pytest.mark.parametrize("use_stringized", [False, True])
def test_capture_with_extras(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    dep_a = plan_fixture_module.mk_external_plan("a")
    dep_b = plan_fixture_module.mk_external_plan("b")

    plan = (
        plan_helpers.build_plan(
            plan_fixture_module.dynamically_make_dynamic_foo_impl,
            interface_spec=core.spec_of(plan_fixture_module.FooInterface[int, str]),
            requires="fxtr_testdeps_fake>=1.0.0",  # <- explicit requries
        )
        .with_runtime_deps([dep_a])
        .with_runtime_deps([dep_b])
        .bind("greetings")  # <- positional argument, bound via signature
    )
    assert plan.constructor == f"{plan_fixture_module.__name__}:dynamically_make_dynamic_foo_impl"
    assert plan.requires == "fxtr_testdeps_fake>=1.0.0"
    assert plan.interface_spec == core.spec_of(plan_fixture_module.FooInterface[int, str])
    assert plan.impl_deps == frozenset([dep_a, dep_b])
    assert plan.kwargs == frozendict({"z": "greetings"})


def test_capture_mutable_fails():
    with pytest.raises(
        ValueError, match="Stateful interfaces cannot be directly instantiated via fxtr plans"
    ):
        plan_helpers.build_plan(plan_fixtures.MutableIncrementerImpl)


@pytest.mark.parametrize("use_stringized", [False, True])
def test_parses_runtime_deps(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    _ext = plan_fixture_module.mk_external_plan

    plan = plan_helpers.build_plan(plan_fixture_module.make_with_inferred_runtime_deps).bind(
        _ext("runtime_plan_1"),
        _ext("not_runtime_plan"),
        tuple_of_dict_of_plans=(
            frozendict(
                {
                    "a": (_ext("tdp_0"), None),
                    "b": (_ext("tdp_1_0"), _ext("tdp_1_1")),
                }
            ),
            _ext("tdp_second"),
        ),
        extract_plan_dynamically=42,
        some_other_keyword_arg=_ext("some_other_keyword_arg"),
    )

    assert plan.constructor == f"{plan_fixture_module.__name__}:make_with_inferred_runtime_deps"

    assert plan.kwargs == frozendict(
        {
            "runtime_plan_1": _ext("runtime_plan_1"),
            "not_runtime_plan": _ext("not_runtime_plan"),
            "tuple_of_dict_of_plans": (
                frozendict(
                    {
                        "a": (_ext("tdp_0"), None),
                        "b": (_ext("tdp_1_0"), _ext("tdp_1_1")),
                    }
                ),
                _ext("tdp_second"),
            ),
            "extract_plan_dynamically": 42,
            "_default_runtime_plan": _ext(
                "_default_runtime_plan"
            ),  # <- bound due to runtime annotation
            "some_other_keyword_arg": _ext("some_other_keyword_arg"),
        }
    )
    assert plan.impl_deps == frozenset(
        {
            _ext("runtime_plan_1"),
            # _ext("not_runtime_plan"),  <- not annotated as a runtime dep
            _ext("tdp_0"),
            _ext("tdp_1_0"),
            _ext("tdp_1_1"),
            # _ext("tdp_second"),  <- not annotated as a runtime dep
            _ext("some_other_keyword_arg"),
            _ext("dynamic_a"),  # <- from the "extract_plan_dynamically" annotation
            _ext("dynamic_b"),  # <- from the "extract_plan_dynamically" annotation
            _ext("_default_runtime_plan"),  # <- from the default value of a keyword argument
            # _ext("_default_other_value"),  <- not annotated as a runtime dep
            _ext("some_other_keyword_arg"),  # <- from the "**kwargs"
        }
    )


@pytest.mark.parametrize("use_stringized", [False, True])
def test_unsupported_runtime_dep_found(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    with pytest.raises(
        plan_helpers.RuntimeDepExtractionError,
        match="Unextractable DependencyAtRuntime annotation found",
    ):
        plan_helpers.build_plan(plan_fixture_module.make_with_unextractable_runtime_dep_ann).bind(
            unextractable_runtime_dep_ann=((3,),)
        )


@pytest.mark.parametrize("use_stringized", [False, True])
def test_task_function_helper(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    dep_plan = plan_fixture_module.mk_external_plan("dep")
    task = plan_fixture_module.my_test_task_function.bind(x=42, dep=dep_plan)
    assert isinstance(task, core.Task)
    plan = task.function
    assert isinstance(plan, py_impl_plan.PyImplPlan)
    assert (
        plan.constructor == f"{plan_fixture_module.__name__}:my_test_task_function.create_task_fn"
    )
    assert plan.requires == "fxtr_testdeps"
    assert plan.interface_spec == core.spec_of(core.TaskFunction[core.AdHocMetadata])
    assert plan.impl_deps == frozenset([dep_plan])
    assert plan.kwargs == frozendict({"x": 42, "dep": dep_plan})


@pytest.mark.parametrize("use_stringized", [False, True])
def test_generic_task_function(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    invalue = core.AdHocMetadata(metadata=core.freeze({"x": 42}))
    task = plan_fixture_module.my_generic_task_function.bind(x=invalue)
    assert isinstance(task, core.Task)
    plan = task.function
    assert isinstance(plan, py_impl_plan.PyImplPlan)
    assert (
        plan.constructor
        == f"{plan_fixture_module.__name__}:my_generic_task_function.create_task_fn"
    )
    assert plan.requires == "fxtr_testdeps"
    assert plan.interface_spec == core.spec_of(core.TaskFunction[Any])
    assert plan.impl_deps == frozenset()
    assert plan.kwargs == frozendict({"x": invalue})


@pytest.mark.parametrize("use_stringized", [False, True])
def test_generic_task_function_with_explicit_binding(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    invalue = core.AdHocMetadata(metadata=core.freeze({"x": 42}))
    task = plan_fixture_module.my_generic_task_function.with_return_type(core.AdHocMetadata).bind(
        x=invalue
    )
    assert isinstance(task, core.Task)
    plan = task.function
    assert isinstance(plan, py_impl_plan.PyImplPlan)
    assert (
        plan.constructor
        == f"{plan_fixture_module.__name__}:my_generic_task_function.create_task_fn"
    )
    assert plan.requires == "fxtr_testdeps"
    assert plan.interface_spec == core.spec_of(core.TaskFunction[core.AdHocMetadata])
    assert plan.impl_deps == frozenset()
    assert plan.kwargs == frozendict({"x": invalue})


@pytest.mark.parametrize("use_stringized", [False, True])
def test_parameterized_retval_task_function(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    invalue = plan_fixture_module.Wrapper(foo=core.AdHocMetadata(metadata=core.freeze({"x": 42})))
    task = plan_fixture_module.my_parameterized_retval_task_function.bind(x=invalue)
    assert isinstance(task, core.Task)
    plan = task.function
    assert isinstance(plan, py_impl_plan.PyImplPlan)
    assert (
        plan.constructor
        == f"{plan_fixture_module.__name__}:my_parameterized_retval_task_function.create_task_fn"
    )
    assert plan.requires == "fxtr_testdeps"
    assert plan.interface_spec == core.spec_of(core.TaskFunction[plan_fixture_module.Wrapper[Any]])
    assert plan.impl_deps == frozenset()
    assert plan.kwargs == frozendict({"x": invalue})


@pytest.mark.parametrize("use_stringized", [False, True])
def test_parameterized_retval_task_function_with_explicit_binding(use_stringized: bool):
    plan_fixture_module = _get_plan_fixture_module(use_stringized)

    invalue = plan_fixture_module.Wrapper(foo=core.AdHocMetadata(metadata=core.freeze({"x": 42})))
    task = plan_fixture_module.my_parameterized_retval_task_function.with_return_type(
        plan_fixture_module.Wrapper[core.AdHocMetadata]
    ).bind(x=invalue)
    assert isinstance(task, core.Task)
    plan = task.function
    assert isinstance(plan, py_impl_plan.PyImplPlan)
    assert (
        plan.constructor
        == f"{plan_fixture_module.__name__}:my_parameterized_retval_task_function.create_task_fn"
    )
    assert plan.requires == "fxtr_testdeps"
    assert plan.interface_spec == core.spec_of(
        core.TaskFunction[plan_fixture_module.Wrapper[core.AdHocMetadata]]
    )
    assert plan.impl_deps == frozenset()

    # note: the typechecker should complain about the below line, but it will work at runtime
    # plan_fixture_module.my_parameterized_retval_task_function.with_return_type(core.AdHocMetadata)
