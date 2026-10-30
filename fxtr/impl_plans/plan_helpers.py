from __future__ import annotations

import dataclasses
import functools
import importlib.metadata
import inspect
import json
import logging
import pathlib
import sys
import types
import typing
from typing import Annotated, Any, Awaitable, Callable, Collection, Concatenate, Coroutine

from frozendict import frozendict

from ..object_model import core
from ..providers import in_process_py_provider
from .py_impl_plan import PyImplPlan

logger = logging.getLogger(__name__)


@functools.cache
def _infer_requires_from_module(mod: str) -> str:
    """Infer a requirement string (distribution package name) from a module name."""
    # Get the root module name (first part before any dots)
    root_module = mod.split(".")[0]

    # First, try looking it up in importlib.metadata.packages_distributions
    packages_distributions = importlib.metadata.packages_distributions()

    if root_module in packages_distributions:
        distributions = packages_distributions[root_module]
        if len(distributions) != 1:
            raise ValueError(
                f"Module '{root_module}' maps to multiple distribution packages: {distributions}. "
                "Cannot uniquely determine the requirement string."
            )
        logger.info(
            f"Found requirement string for {root_module} in"
            f" importlib.metadata.packages_distributions: {distributions[0]}"
        )
        return distributions[0]

    # If not found in packages_distributions, check if the module is loaded
    # and try to find an editable install via direct_url.json
    if root_module not in sys.modules:
        raise ValueError(
            f"Could not infer requirement string for module '{mod}'. The module '{root_module}' is"
            " not found in importlib.metadata.packages_distributions() and is not loaded in"
            " sys.modules."
        )

    module_obj = sys.modules[root_module]
    module_file = getattr(module_obj, "__file__", None)

    if not module_file:
        raise ValueError(
            f"Could not infer requirement string for module '{mod}'. The module '{root_module}' "
            "has no __file__ attribute."
        )

    module_path = pathlib.Path(module_file).resolve()

    # Check editable installs, looking specifically for the root module name as distribution
    def check_dist(dist: importlib.metadata.Distribution) -> str | None:
        if not any(f.name == "direct_url.json" for f in dist.files or ()):
            return
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            direct_url_json = json.loads(direct_url_text)

            # Check if this is an editable install
            if not direct_url_json.get("dir_info", {}).get("editable"):
                return

            # Get the directory path from the direct_url
            url = direct_url_json.get("url", "")
            if url.startswith("file://"):
                dist_path = pathlib.Path(url.removeprefix("file://")).resolve()

                # Check if the module file is under this distribution path
                if module_path.is_relative_to(dist_path):
                    logger.info(f"Found local editable distribution for {root_module}: {dist.name}")
                    return dist.name

    try:
        this_dist = importlib.metadata.distribution(root_module)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        res = check_dist(this_dist)
        if res == root_module:
            return root_module

    matching_dists = []
    for dist in importlib.metadata.distributions():
        found_dist_name = check_dist(dist)
        if found_dist_name is not None:
            matching_dists.append(found_dist_name)

    if len(matching_dists) > 1:
        raise ValueError(
            f"Module '{root_module}' matches multiple editable distributions, none of which match"
            f" the root module name: {matching_dists}. Cannot uniquely determine the requirement"
            " string."
        )

    if matching_dists:
        return matching_dists[0]

    # Could not determine the requirement string
    raise ValueError(
        f"Could not infer requirement string for module '{mod}'. The module '{root_module}' "
        "is not found in importlib.metadata.packages_distributions() and could not be "
        "matched to an editable install with the same name."
    )


@dataclasses.dataclass(frozen=True)
class DependencyAtRuntime:
    """Annotation marker indicating that a plan is expected to be provided as a runtime dependency.

    Can be used as Annotated[Plan[T], DependencyAtRuntime()] to indicate that the plan is expected to
    be provided as a runtime dependency. For additional flexibility, it is also possible to specify
    a conversion function, which will accept the argment and return a collection of plans
    that are expected to be provided as runtime dependencies. If provided, the conversion function
    must be a deterministic function of its argument only.

    Args:
        plan_extractor: A function that will accept the argument and return a collection of plans
            that are expected to be provided as runtime dependencies.
    """

    plan_extractor: Callable[[Any], Collection[core.Plan[Any]]] | None = None


type RuntimeDepPlan[T: core.FxtrCallTarget] = Annotated[core.Plan[T], DependencyAtRuntime()]
"""Type alias for a plan that is expected to be provided as a runtime dependency."""


class RuntimeDepExtractionError(ValueError):
    """Exception raised when a runtime requirement extraction fails."""


def _check_no_runtime_dep_plans_in_annotation(annotation: Any) -> None:
    """Check if an annotation contains any required plans that are expected to be provided as runtime dependencies.

    Args:
        annotation: The annotation to check.
    """
    ann_origin = typing.get_origin(annotation) or annotation
    ann_args = typing.get_args(annotation)
    if isinstance(ann_origin, typing.TypeAliasType):
        try:
            _check_no_runtime_dep_plans_in_annotation(ann_origin.__value__)
        except RuntimeDepExtractionError as e:
            e.add_note(f"When desugaring type alias {annotation}")
            raise
    elif ann_origin is Annotated:
        for annotation_value in ann_args[1:]:
            if isinstance(annotation_value, DependencyAtRuntime):
                raise RuntimeDepExtractionError(
                    f"Unextractable DependencyAtRuntime annotation found in {annotation}"
                )
        try:
            _check_no_runtime_dep_plans_in_annotation(ann_args[0])
        except RuntimeDepExtractionError as e:
            e.add_note(f"At the first type parameter of {annotation}")
            raise
    else:
        for i, arg in enumerate(ann_args):
            try:
                _check_no_runtime_dep_plans_in_annotation(arg)
            except RuntimeDepExtractionError as e:
                e.add_note(f"At type parameter index {i} of {annotation}")
                raise


def _parse_runtime_dep_plans(value: Any, annotation: Any) -> Collection[core.Plan[Any]]:
    """Extract plans that are expected to be provided at runtime from an annotated argument.

    Args:
        value: The argument to extract plans from.
        annotation: The annotation for the parameter, that indicates whether the argument is
            expected to be provided at runtime.

    Returns: A collection of plans that are expected to be provided at runtime.

    Raises: RuntimeDepExtractionError if the type annotation contains RuntimeDepPlan or
        DependencyAtRuntime annotations, but we could not successfully extract the plans from the
        argument.
    """
    if annotation is inspect.Parameter.empty:
        return ()

    ann_origin = typing.get_origin(annotation) or annotation
    ann_args = typing.get_args(annotation)

    # Desugar RuntimeDepPlan annotation
    if ann_origin is RuntimeDepPlan:
        return _parse_runtime_dep_plans(value, RuntimeDepPlan.__value__)

    # Handle DependencyAtRuntime
    if ann_origin is Annotated:
        found_reqs = [v for v in ann_args[1:] if isinstance(v, DependencyAtRuntime)]
        if not found_reqs:
            return _parse_runtime_dep_plans(value, ann_args[0])
        elif len(found_reqs) > 1:
            raise ValueError(f"Multiple DependencyAtRuntime annotations found in {annotation}")
        else:
            req = found_reqs[0]
            if req.plan_extractor is None:
                return {value}
            else:
                return req.plan_extractor(value)

    # Handle a limited set of unions
    if ann_origin in (typing.Union, types.UnionType):
        # Unions of required plans and None can be handled specially
        is_allowed = True
        for arg in ann_args:
            if (typing.get_origin(arg) or arg) is RuntimeDepPlan:
                continue
            elif arg is None or arg is types.NoneType:
                continue
            else:
                is_allowed = False
                break
        if is_allowed:
            # If we got this far, every element of the union is a required plan
            if value is None:
                return ()
            else:
                return (value,)

    # Handle tuples
    if ann_origin in (tuple, typing.Tuple):
        if not isinstance(value, tuple):
            raise ValueError(f"Expected tuple, got {type(value)}")
        if len(ann_args) == 2 and ann_args[1] is ...:
            acc = set()
            for subvalue in value:
                acc.update(_parse_runtime_dep_plans(subvalue, ann_args[0]))
            return acc
        else:
            assert len(ann_args) == len(value)
            acc = set()
            for i, subvalue in enumerate(value):
                acc.update(_parse_runtime_dep_plans(subvalue, ann_args[i]))
            return acc

    # Handle frozendicts
    if ann_origin is frozendict and len(ann_args) == 2:
        if not isinstance(value, frozendict):
            raise ValueError(f"Expected frozendict, got {type(value)}")
        key_ann, value_ann = ann_args
        acc = set()
        for key, subvalue in value.items():
            # TODO: Key annotations are not tested, because fxtr doesn't actually allow frozendict
            # keys to be non-string types currently; this is going to be fixed in the future, at
            # which point we need to make sure this works.
            acc.update(_parse_runtime_dep_plans(key, key_ann))
            acc.update(_parse_runtime_dep_plans(subvalue, value_ann))
        return acc

    # Otherwise, make sure this plan is actually safe to _not_ materialize as a runtime dependency
    _check_no_runtime_dep_plans_in_annotation(annotation)
    return ()


@dataclasses.dataclass(kw_only=True)
class PyImplPlanHelper[T: core.FxtrCallTarget, **P]:
    """A helper for constructing a Python implementation plan."""

    constructor: str | None = None
    requires: str | None = None
    interface_spec: core.InterfaceSpec[T] | None = None
    impl_deps: frozenset[core.Plan[Any]] = frozenset()
    _signature: inspect.Signature | None = None

    def with_constructor[
        T2: core.FxtrCallTarget, **P2
    ](
        self,
        constructor: Callable[Concatenate[core.PlanResolver, P2], Any] | str,
        *,
        requires: str | None = None,
        interface_spec: core.InterfaceSpec[T2] | None = None,
    ) -> PyImplPlanHelper[T2, P2]:
        """Set the constructor for the plan.

        Args:
            constructor: The constructor to use for the plan. Can be a callable or a string
                representing the entrypoint of a callable in a module. In either case, the result
                of calling the constructor must be a deterministic function of its arguments.
                Fxtr assumes it can safely call the constructor multiple times to construct an
                equivalent call target, and it may also share a single instance of the call target
                between multiple tasks.
            requires: The requirements string for the plan. If not provided, will be inferred from
                the module of the constructor.
            interface_spec: The interface spec for the plan. If not provided, will be inferred from
                the constructor.

        Returns: A new PyImplPlanHelper with the constructor set, which can be turned into a plan
            using `bind`.
        """
        if self.constructor is not None:
            raise ValueError("`constructor` was already provided before and cannot be changed!")

        if requires is None:
            requires = self.requires
        elif self.requires is None:
            pass
        elif self.requires != requires:
            raise ValueError("`requires` was provided both before and in `with_constructor`!")

        if isinstance(constructor, str):
            ctor_str = constructor
            signature = None
        else:
            unwrapped_ctor = inspect.unwrap(constructor)
            mod = getattr(unwrapped_ctor, "__module__", None)
            qualname = getattr(unwrapped_ctor, "__qualname__", None)
            if mod is None or qualname is None or ("<locals>" in qualname):
                raise ValueError(
                    "Callable constructors must be exposed functions or classes (having"
                    " `__module__` and `__qualname__` attributes) in order to be converted into a"
                    " plan!"
                )
            if mod == "__main__":
                raise ValueError(
                    "Cannot capture a constructor from the main module; please expose it as a"
                    " function or class in a separate importable module!"
                )
            ctor_str = f"{mod}:{qualname}"
            ep = in_process_py_provider._load_object_from_entrypoint(ctor_str)
            if ep != constructor:
                raise ValueError(
                    f"Constructor {constructor} advertises itself as being available at entrypoint"
                    f" string {ctor_str}, but it does not match the object {ep} that is actually"
                    " present there!"
                )
            signature = inspect.signature(constructor, eval_str=True)
            for param in list(signature.parameters.values())[1:]:
                if (
                    param.kind == inspect.Parameter.POSITIONAL_ONLY
                    or param.kind == inspect.Parameter.VAR_POSITIONAL
                ):
                    raise ValueError(
                        "Positional-only or var-positional parameters are not supported in"
                        " constructor signatures; all arguments (other than implicit first"
                        " resolver) must be passable by keyword!"
                    )

            if requires is None and mod is not None:
                # Try to infer requires string
                requires = _infer_requires_from_module(mod)
            if interface_spec is None:
                try:
                    # Try to infer interface spec from class
                    if isinstance(constructor, type):
                        interface_spec = core.spec_of_concrete_target(constructor)
                    # Try to infer interface spec from return annotation.
                    elif signature.return_annotation is not inspect.Signature.empty:
                        interface_spec = core.spec_of_concrete_target(signature.return_annotation)
                    else:
                        raise ValueError(
                            "Could not infer interface spec from constructor (not a class and no"
                            " return type annotation). Please add type annotations or specify the"
                            " interface spec manually!"
                        )
                except Exception as e:
                    e.add_note(
                        f"While inferring interface spec of plan from constructor {ctor_str}"
                    )
                    raise

        if requires is None:
            raise ValueError(
                "`requires` was not provided before and could not infer it from constructor!"
            )
        if interface_spec is None:
            raise ValueError(
                "`interface_spec` was not provided before and could not infer it from constructor!"
            )
        if not interface_spec.defn.stateless:
            raise ValueError(
                f"Interface definition {interface_spec.defn.interface_id} is not stateless! "
                "Stateful interfaces cannot be directly instantiated"
                " via fxtr plans; please instead use the plan to construct an immutable 'factory'"
                " call target that dynamically constructs mutable instances."
            )

        return typing.cast(
            PyImplPlanHelper[T2, P2],
            dataclasses.replace(
                self,
                constructor=ctor_str,
                requires=requires,
                interface_spec=interface_spec,
                _signature=signature,
            ),
        )

    def with_requires(self, requires: str) -> PyImplPlanHelper[T, P]:
        if self.requires is not None:
            raise ValueError("`requires` was already provided before and cannot be changed!")
        return dataclasses.replace(self, requires=requires)

    def with_runtime_deps(self, runtime_deps: Collection[core.Plan[Any]]) -> PyImplPlanHelper[T, P]:
        new_deps = self.impl_deps | frozenset(runtime_deps)
        return dataclasses.replace(self, impl_deps=new_deps)

    def bind(self, *args: P.args, **kwargs: P.kwargs) -> PyImplPlan[T]:
        """Bind arguments to a plan as keyword arguments.

        This function can be used to construct the keyword arguments for a plan object by binding
        the provided arguments based on the constructor signature. Additionally, if a signature is
        known (because the constructor was a Python callable), and any of the types are annotated
        with the special annotations RuntimeDepPlan[T] or Annotated[..., DependencyAtRuntime()],
        then the plans that are expected to be provided at runtime will be extracted from the
        arguments and added to the plan's implementation dependencies.

        As a special case, if the constructor has default arguments, and those default arguments
        are themselves annotated as having runtime dependencies, then the default values will be
        lifted out of the default and stored as runtime dependencies and also as explicit keyword
        arguments to the plan. This is to ensure that those runtime dependencies are available to
        the plan when called, even if not explicitly bound. (Note that other default arguments are
        _not_ lifted in this way, because we do not want to introduce breaking changes in old plan
        instances when adding new default arguments. However, adding new default arguments that
        declare runtime dependencies are always breaking changes due to requiring the caller to
        provide those dependencies at runtime, so they are assumed to be safe to inline this way.)

        Args:
            args: The positional arguments to bind to the plan.
            kwargs: The keyword arguments to bind to the plan.

        Returns: A new PyImplPlan with the arguments bound.

        Raises:
            ValueError or RuntimeDepExtractionError: if we cannot successfully interpret the
                provided arguments based on the known signature.
        """
        extra_impl_deps = set()
        if not self.constructor:
            raise ValueError("Cannot bind arguments to a plan without a constructor!")
        if self._signature:
            dummy_resolver = object()
            bound = self._signature.bind(dummy_resolver, *args, **kwargs)
            sigparams = list(self._signature.parameters.items())
            bound_kwargs = {}
            for name, param in sigparams[1:]:
                if param.kind == inspect.Parameter.VAR_KEYWORD:
                    if name in bound.arguments:
                        for k, v in bound.arguments[name].items():
                            try:
                                extra_impl_deps.update(
                                    _parse_runtime_dep_plans(v, param.annotation)
                                )
                            except RuntimeDepExtractionError as e:
                                e.add_note(f"While binding argument {repr(k)}")
                                raise
                            bound_kwargs[k] = v
                else:
                    assert param.kind in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    )
                    if name in bound.arguments:
                        try:
                            extra_impl_deps.update(
                                _parse_runtime_dep_plans(bound.arguments[name], param.annotation)
                            )
                        except RuntimeDepExtractionError as e:
                            e.add_note(f"While binding argument {repr(name)}")
                            raise
                        bound_kwargs[name] = bound.arguments[name]
                    elif param.default is not inspect.Parameter.empty:
                        try:
                            default_deps = _parse_runtime_dep_plans(param.default, param.annotation)
                        except RuntimeDepExtractionError as e:
                            e.add_note(f"While binding default value of argument {repr(name)}")
                            raise
                        if default_deps:
                            extra_impl_deps.update(default_deps)
                            bound_kwargs[name] = param.default
        elif args:
            raise ValueError(
                "Could not infer names from positional arguments: constructor does not have a"
                " known signature!"
            )
        else:
            bound_kwargs = kwargs

        assert self.requires is not None
        assert self.interface_spec is not None

        return PyImplPlan(
            constructor=self.constructor,
            requires=self.requires,
            interface_spec=self.interface_spec,
            impl_deps=self.impl_deps | extra_impl_deps,
            kwargs=frozendict(bound_kwargs),
        )


def build_plan[
    T: core.FxtrCallTarget, **P
](
    constructor: str | Callable[Concatenate[core.PlanResolver, P], Any],
    *,
    requires: str | None = None,
    interface_spec: core.InterfaceSpec[T] | None = None,
) -> PyImplPlanHelper[T, P]:
    """Create a plan builder helper for a callable.

    Args:
        constructor: The constructor to use for the plan. Can be a callable or a string
            representing the entrypoint of a callable in a module. In either case, the result
            of calling the constructor must be a deterministic function of its arguments.
            Fxtr assumes it can safely call the constructor multiple times to construct an
            equivalent call target, and it may also share a single instance of the call target
            between multiple tasks.
        requires: The requirements string for the plan. If not provided, will be inferred from
            the module of the constructor.
        interface_spec: The interface spec for the plan. If not provided, will be inferred from
            the constructor.

    Returns: A new PyImplPlanHelper with the constructor set, which can be turned into a plan
        using `bind`.
    """
    return PyImplPlanHelper().with_constructor(
        constructor, requires=requires, interface_spec=interface_spec
    )


class _DynamicTaskFunctionImpl[R: core.AnyEntity, **P](core.TaskFunction[R]):
    """A task function that is constructed from a helper."""

    def __init__(
        self,
        fn: Callable[Concatenate[core.PlanResolver, P], Coroutine[Any, Any, R]],
        resolver: core.PlanResolver,
        interface_spec: core.InterfaceSpec[core.TaskFunction[R]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ):
        self._fn = fn
        self._resolver = resolver
        self._interface_spec = interface_spec
        self._args = args
        self._kwargs = kwargs

    def interface_spec(self) -> core.InterfaceSpec[core.TaskFunction[R]]:
        return self._interface_spec

    async def run_task(self) -> R:
        return await self._fn(self._resolver, *self._args, **self._kwargs)


@dataclasses.dataclass(frozen=True)
class TaskFunctionWrapper[R: core.AnyEntity, **P]:
    """A helper for constructing a task function plan."""

    fn: Callable[Concatenate[core.PlanResolver, P], Coroutine[Any, Any, R]]
    module_name: str
    qualname: str
    requires: str
    default_interface_spec: core.InterfaceSpec[core.TaskFunction[R]]

    def create_task_fn(
        self, resolver: core.PlanResolver, /, *args: P.args, **kwargs: P.kwargs
    ) -> core.TaskFunction[R]:
        """Creates the task function that will run the task. Usually only used internally by fxtr.

        Args:
            resolver: The resolver to use for the task.
            args: The positional arguments to pass to the task.
            kwargs: The keyword arguments to pass to the task.

        Returns: A task function that will run the task.
        """
        return _DynamicTaskFunctionImpl(
            self.fn, resolver, self.default_interface_spec, *args, **kwargs
        )

    def with_return_type(self, return_type: type[R]) -> TaskFunctionBindHelper[R, P]:
        """Annotate the specific return type of the task function.

        This is most useful in situations where the return type of the task function is a generic
        type that cannot be inferred directly from the function signature.

        Args:
            return_type: The type of the return value of the task function.

        Returns: A new wrapper, where `.bind` produces a task with the specified return type.
        """
        return TaskFunctionBindHelper(self, core.spec_of(core.TaskFunction[return_type]))

    def bind(self, *args: P.args, **kwargs: P.kwargs) -> core.Task[R]:
        """Creates a task by binding the provided arguments to the wrapped task function.

        Args:
            args: The positional arguments to pass to the task.
            kwargs: The keyword arguments to pass to the task.

        Returns: A task object that runs the provided task function.
        """
        return TaskFunctionBindHelper(self, self.default_interface_spec).bind(*args, **kwargs)

    async def __call__(
        self, resolver: core.PlanResolver, /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        return await self.fn(resolver, *args, **kwargs)

    @property
    def __wrapped__(self) -> Callable[Concatenate[core.PlanResolver, P], Coroutine[Any, Any, R]]:
        return self.fn


@dataclasses.dataclass(frozen=True)
class TaskFunctionBindHelper[R: core.AnyEntity, **P]:
    """A helper for constructing a task function plan with a specific return type."""

    wrapper: TaskFunctionWrapper[R, P]
    interface_spec: core.InterfaceSpec[core.TaskFunction[R]]

    def bind(self, *args: P.args, **kwargs: P.kwargs) -> core.Task[R]:
        """Creates a task by binding the provided arguments to the wrapped task function.

        Args:
            args: The positional arguments to pass to the task.
            kwargs: The keyword arguments to pass to the task.

        Returns: A task object that runs the provided task function.
        """
        wrapper = self.wrapper
        retrieved_ctor = in_process_py_provider._load_object_from_entrypoint(
            f"{wrapper.module_name}:{wrapper.qualname}.create_task_fn"
        )
        if retrieved_ctor != wrapper.create_task_fn:
            retrieved_wrapper = in_process_py_provider._load_object_from_entrypoint(
                f"{wrapper.module_name}:{wrapper.qualname}"
            )
            if retrieved_wrapper != wrapper:
                raise ValueError(
                    "Task function advertises itself as being available at entrypoint string"
                    f" {wrapper.module_name}:{wrapper.qualname}, but it does not match the"
                    f" object {retrieved_wrapper} that is actually present there!"
                )
            else:
                raise RuntimeError(
                    "Internal error: got unexpected `create_task_fn` binding: got"
                    f" {retrieved_ctor} but expected {wrapper.create_task_fn}"
                )

        orig_sig = inspect.signature(wrapper.fn, eval_str=True)
        if orig_sig.return_annotation is not inspect.Signature.empty:
            sig = orig_sig.replace(return_annotation=core.TaskFunction[orig_sig.return_annotation])
        else:
            sig = orig_sig

        plan = PyImplPlanHelper(
            constructor=f"{wrapper.module_name}:{wrapper.qualname}.create_task_fn",
            requires=wrapper.requires,
            interface_spec=self.interface_spec,
            _signature=sig,
        ).bind(*args, **kwargs)

        return core.Task(function=plan)


@typing.overload
def task_function[
    R: core.AnyEntity, **P
](
    *,
    requires: str | None = None,
    result_type: type[R] | None = None,
) -> Callable[
    [Callable[Concatenate[core.PlanResolver, P], Coroutine[Any, Any, R]]], TaskFunctionWrapper[R, P]
]: ...


@typing.overload
def task_function[
    R: core.AnyEntity, **P
](
    fn: Callable[Concatenate[core.PlanResolver, P], Coroutine[Any, Any, R]],
    /,
    requires: str | None = None,
    result_type: type[R] | None = None,
) -> TaskFunctionWrapper[R, P]: ...


def task_function[
    R: core.AnyEntity, **P
](
    fn_or_none: Callable[Concatenate[core.PlanResolver, P], Coroutine[Any, Any, R]] | None = None,
    /,
    requires: str | None = None,
    result_type: type[R] | None = None,
) -> (
    TaskFunctionWrapper[R, P]
    | Callable[
        [Callable[Concatenate[core.PlanResolver, P], Coroutine[Any, Any, R]]],
        TaskFunctionWrapper[R, P],
    ]
):
    """Decorator to wrap a callable as a task function.

    This decorator registers a function as a fxtr TaskFunction, and also adds methods for creating
    Task objects that run the function. It can be used like

    ```python
    @task_function
    async def my_test_task_function(
        resolver: core.PlanResolver,
        previous: RuntimeDepPlan[StorageHandle[Foo]],
        bar: int
    ) -> Baz:
        ...
        return result

    this_task = my_test_task_function.bind(previous=previous_task.result_handle, bar=3)
    ```

    The result (e.g. `my_test_task_function` above) can also be called directly as normal.
    Additionally, the method `my_test_task_function.create_task_function` can be used to create a
    TaskFunction impl (TaskFunction[Baz] in this case) that can be used to run the task.

    Args:
        fn_or_none: The function to wrap. If None, a decorator will be returned that can be used to
            wrap a function.
        requires: The requirements string for the task function. If not provided, will be inferred
            from the module of the function.
        result_type: The type of the result of the task function. If not provided, will be inferred
            from the return type of the function.
    """

    def decorator(
        fn: Callable[Concatenate[core.PlanResolver, P], Coroutine[Any, Any, R]],
    ) -> TaskFunctionWrapper[R, P]:

        unwrapped_fn = inspect.unwrap(fn)
        if requires is None:
            fn_requires = _infer_requires_from_module(unwrapped_fn.__module__)
        else:
            fn_requires = requires

        if result_type is None:
            fn_annotations = inspect.get_annotations(unwrapped_fn, eval_str=True)
            if "return" not in fn_annotations:
                raise ValueError(
                    "Callable does not have a return type annotation and no explicit result type"
                    " was provided"
                )
            fn_result_type = fn_annotations["return"]
            if isinstance(fn_result_type, typing.TypeVar):
                fn_result_type = Any
        else:
            fn_result_type = result_type

        if fn_result_type is Any:
            # funnily enough pyright is OK with TaskFunction[Any] but is not ok with
            # TaskFuntion[fn_result_type] here, even though they are identical at runtime.
            interface_spec = core.spec_of(core.TaskFunction[Any])
        else:
            logger.debug(f"fn_result_type: {fn_result_type}")
            fn_result_type_base = typing.cast(
                Any, typing.get_origin(fn_result_type) or fn_result_type
            )
            if not (
                isinstance(fn_result_type_base, type)
                and issubclass(fn_result_type_base, core.EntityBase)
            ):
                raise TypeError(f"Task functions must return fxtr entities, not {fn_result_type}")
            fn_result_type = typing.cast(Any, fn_result_type)
            interface_spec = core._spec_of_with_mode(
                core.TaskFunction[fn_result_type], tv_mode="as_any"
            )

        return TaskFunctionWrapper(
            fn=unwrapped_fn,
            module_name=unwrapped_fn.__module__,
            qualname=unwrapped_fn.__qualname__,
            requires=fn_requires,
            default_interface_spec=interface_spec,
        )

    if fn_or_none is not None:
        return decorator(fn_or_none)
    else:
        return decorator


def _extract_workflow_types(
    return_type: Any,
) -> tuple[Any, Any]:
    """Extract state and result types from a WorkflowState[S, R] | WorkflowResult[R] union type,
    or from a single WorkflowState[S, R] or WorkflowResult[R] type.

    Args:
        return_type: The return type annotation, which can be:
            - A Union of WorkflowState and WorkflowResult
            - A single WorkflowState[S, R]
            - A single WorkflowResult[R]

    Returns:
        A tuple of (state_type, result_type). If types cannot be extracted, returns (None, None).
    """
    origin = typing.get_origin(return_type) or return_type

    # Handle Union type
    if origin is typing.Union:
        union_args = typing.get_args(return_type)
        if len(union_args) != 2:
            return (None, None)

        extracted_state_type: Any = None
        extracted_result_type: Any = None

        # Try to extract from WorkflowState[S, R] and WorkflowResult[R]
        for arg in union_args:
            arg_origin = typing.get_origin(arg) or arg
            if arg_origin is core.WorkflowState:
                state_args = typing.get_args(arg)
                if len(state_args) == 2:
                    extracted_state_type = state_args[0]
                    extracted_result_type = state_args[1]
            elif arg_origin is core.WorkflowResult:
                result_args = typing.get_args(arg)
                if len(result_args) == 1:
                    extracted_result_type = result_args[0]

        return (extracted_state_type, extracted_result_type)

    # Handle single WorkflowState[S, R]
    elif origin is core.WorkflowState:
        state_args = typing.get_args(return_type)
        if len(state_args) == 2:
            return (state_args[0], state_args[1])
        return (None, None)

    # Handle single WorkflowResult[R]
    elif origin is core.WorkflowResult:
        result_args = typing.get_args(return_type)
        if len(result_args) == 1:
            return (None, result_args[0])
        return (None, None)

    # Not a workflow type
    return (None, None)


class _DynamicWorkflowFunctionImpl[S: core.AnyFrozenIPLDable, R: core.AnyEntity, **P](
    core.WorkflowFunction[S, R]
):
    """A workflow function that is constructed from a helper."""

    def __init__(
        self,
        fn: Callable[
            Concatenate[core.PlanResolver, P],
            Coroutine[Any, Any, core.WorkflowState[S, R] | core.WorkflowResult[R]],
        ],
        resolver: core.PlanResolver,
        interface_spec: core.InterfaceSpec[core.WorkflowFunction[S, R]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ):
        self._fn = fn
        self._resolver = resolver
        self._interface_spec = interface_spec
        self._args = args
        self._kwargs = kwargs

    def interface_spec(self) -> core.InterfaceSpec[core.WorkflowFunction[S, R]]:
        return self._interface_spec

    async def run_step(self) -> core.WorkflowState[S, R] | core.WorkflowResult[R]:
        return await self._fn(self._resolver, *self._args, **self._kwargs)


@dataclasses.dataclass(frozen=True)
class WorkflowFunctionWrapper[S: core.AnyFrozenIPLDable, R: core.AnyEntity, **P]:
    """A helper for constructing a workflow function plan."""

    fn: Callable[
        Concatenate[core.PlanResolver, P],
        Coroutine[Any, Any, core.WorkflowState[S, R] | core.WorkflowResult[R]],
    ]
    module_name: str
    qualname: str
    requires: str
    default_interface_spec: core.InterfaceSpec[core.WorkflowFunction[S, R]]

    def create_workflow_fn(
        self, resolver: core.PlanResolver, /, *args: P.args, **kwargs: P.kwargs
    ) -> core.WorkflowFunction[S, R]:
        """Creates the workflow function that will run the workflow. Usually only used internally by fxtr.

        Args:
            resolver: The resolver to use for the workflow.
            args: The positional arguments to pass to the workflow.
            kwargs: The keyword arguments to pass to the workflow.

        Returns: A workflow function that will run the workflow.
        """
        return _DynamicWorkflowFunctionImpl(
            self.fn, resolver, self.default_interface_spec, *args, **kwargs
        )

    def with_return_type(
        self, state_type: type[S], return_type: type[R]
    ) -> WorkflowFunctionBindHelper[S, R, P]:
        """Annotate the specific return type of the workflow function.

        This is most useful in situations where the return type of the workflow function is a generic
        type that cannot be inferred directly from the function signature.

        Args:
            state_type: The type of the state of the workflow function.
            return_type: The type of the return value of the workflow function.

        Returns: A new wrapper, where `.bind` produces a workflow with the specified return type.
        """
        return WorkflowFunctionBindHelper(
            self, core.spec_of(core.WorkflowFunction[state_type, return_type])
        )

    def bind(self, *args: P.args, **kwargs: P.kwargs) -> core.Workflow[S, R]:
        """Creates a workflow by binding the provided arguments to the wrapped workflow function.

        Args:
            args: The positional arguments to pass to the workflow.
            kwargs: The keyword arguments to pass to the workflow.

        Returns: A workflow object that runs the provided workflow function.
        """
        return WorkflowFunctionBindHelper(self, self.default_interface_spec).bind(*args, **kwargs)

    async def __call__(
        self, resolver: core.PlanResolver, /, *args: P.args, **kwargs: P.kwargs
    ) -> core.WorkflowState[S, R] | core.WorkflowResult[R]:
        return await self.fn(resolver, *args, **kwargs)

    @property
    def __wrapped__(
        self,
    ) -> Callable[
        Concatenate[core.PlanResolver, P],
        Coroutine[Any, Any, core.WorkflowState[S, R] | core.WorkflowResult[R]],
    ]:
        return self.fn


@dataclasses.dataclass(frozen=True)
class WorkflowFunctionBindHelper[S: core.AnyFrozenIPLDable, R: core.AnyEntity, **P]:
    """A helper for constructing a workflow function plan with a specific return type."""

    wrapper: WorkflowFunctionWrapper[S, R, P]
    interface_spec: core.InterfaceSpec[core.WorkflowFunction[S, R]]

    def bind(self, *args: P.args, **kwargs: P.kwargs) -> core.Workflow[S, R]:
        """Creates a workflow by binding the provided arguments to the wrapped workflow function.

        Args:
            args: The positional arguments to pass to the workflow.
            kwargs: The keyword arguments to pass to the workflow.

        Returns: A workflow object that runs the provided workflow function.
        """
        wrapper = self.wrapper
        retrieved_ctor = in_process_py_provider._load_object_from_entrypoint(
            f"{wrapper.module_name}:{wrapper.qualname}.create_workflow_fn"
        )
        if retrieved_ctor != wrapper.create_workflow_fn:
            retrieved_wrapper = in_process_py_provider._load_object_from_entrypoint(
                f"{wrapper.module_name}:{wrapper.qualname}"
            )
            if retrieved_wrapper != wrapper:
                raise ValueError(
                    "Workflow function advertises itself as being available at entrypoint string"
                    f" {wrapper.module_name}:{wrapper.qualname}, but it does not match the"
                    f" object {retrieved_wrapper} that is actually present there!"
                )
            else:
                raise RuntimeError(
                    "Internal error: got unexpected `create_workflow_fn` binding: got"
                    f" {retrieved_ctor} but expected {wrapper.create_workflow_fn}"
                )

        orig_sig = inspect.signature(wrapper.fn, eval_str=True)
        if orig_sig.return_annotation is not inspect.Signature.empty:
            # Extract state and result types from WorkflowState[S, R] | WorkflowResult[R]
            return_ann = orig_sig.return_annotation
            extracted_state_type, extracted_result_type = _extract_workflow_types(return_ann)
            if extracted_state_type is None:
                extracted_state_type = Any
            if extracted_result_type is None:
                extracted_result_type = Any
            sig = orig_sig.replace(
                return_annotation=core.WorkflowFunction[extracted_state_type, extracted_result_type]  # type: ignore
            )
        else:
            sig = orig_sig

        plan = PyImplPlanHelper(
            constructor=f"{wrapper.module_name}:{wrapper.qualname}.create_workflow_fn",
            requires=wrapper.requires,
            interface_spec=self.interface_spec,
            _signature=sig,
        ).bind(*args, **kwargs)

        return core.Workflow(function=plan)


@typing.overload
def workflow_function[
    S: core.AnyFrozenIPLDable, R: core.AnyEntity, **P
](
    *,
    requires: str | None = None,
    state_type: type[S] | None = None,
    result_type: type[R] | None = None,
) -> Callable[
    [
        Callable[
            Concatenate[core.PlanResolver, P],
            Coroutine[Any, Any, core.WorkflowState[S, R] | core.WorkflowResult[R]],
        ]
    ],
    WorkflowFunctionWrapper[S, R, P],
]: ...


@typing.overload
def workflow_function[
    S: core.AnyFrozenIPLDable, R: core.AnyEntity, **P
](
    fn: Callable[
        Concatenate[core.PlanResolver, P],
        Coroutine[Any, Any, core.WorkflowState[S, R] | core.WorkflowResult[R]],
    ],
    /,
    requires: str | None = None,
    state_type: type[S] | None = None,
    result_type: type[R] | None = None,
) -> WorkflowFunctionWrapper[S, R, P]: ...


def workflow_function[
    S: core.AnyFrozenIPLDable, R: core.AnyEntity, **P
](
    fn_or_none: (
        Callable[
            Concatenate[core.PlanResolver, P],
            Coroutine[Any, Any, core.WorkflowState[S, R] | core.WorkflowResult[R]],
        ]
        | None
    ) = None,
    /,
    requires: str | None = None,
    state_type: type[S] | None = None,
    result_type: type[R] | None = None,
) -> (
    WorkflowFunctionWrapper[S, R, P]
    | Callable[
        [
            Callable[
                Concatenate[core.PlanResolver, P],
                Coroutine[Any, Any, core.WorkflowState[S, R] | core.WorkflowResult[R]],
            ]
        ],
        WorkflowFunctionWrapper[S, R, P],
    ]
):
    """Decorator to wrap a callable as a workflow function.

    This decorator registers a function as a fxtr WorkflowFunction, and also adds methods for creating
    Workflow objects that run the function. It can be used like

    ```python
    @workflow_function
    async def my_test_workflow_function(
        resolver: core.PlanResolver,
        previous: RuntimeDepPlan[StorageHandle[Foo]],
        bar: int
    ) -> WorkflowState[MyState, Baz] | WorkflowResult[Baz]:
        ...
        return result

    this_workflow = my_test_workflow_function.bind(previous=previous_workflow.result_handle, bar=3)
    ```

    The result (e.g. `my_test_workflow_function` above) can also be called directly as normal.
    Additionally, the method `my_test_workflow_function.create_workflow_function` can be used to create a
    WorkflowFunction impl (WorkflowFunction[MyState, Baz] in this case) that can be used to run the workflow.

    Args:
        fn_or_none: The function to wrap. If None, a decorator will be returned that can be used to
            wrap a function.
        requires: The requirements string for the workflow function. If not provided, will be inferred
            from the module of the function.
        state_type: The type of the state of the workflow function. If not provided, will be inferred
            from the return type of the function.
        result_type: The type of the result of the workflow function. If not provided, will be inferred
            from the return type of the function.
    """

    def decorator(
        fn: Callable[
            Concatenate[core.PlanResolver, P],
            Coroutine[Any, Any, core.WorkflowState[S, R] | core.WorkflowResult[R]],
        ],
    ) -> WorkflowFunctionWrapper[S, R, P]:

        unwrapped_fn = inspect.unwrap(fn)
        if requires is None:
            fn_requires = _infer_requires_from_module(unwrapped_fn.__module__)
        else:
            fn_requires = requires

        fn_result_type: Any = None
        fn_state_type: Any = None

        if result_type is None or state_type is None:
            fn_annotations = inspect.get_annotations(unwrapped_fn, eval_str=True)
            if "return" not in fn_annotations:
                raise ValueError(
                    "Callable does not have a return type annotation and no explicit result type"
                    " or state type was provided"
                )
            fn_return_type = fn_annotations["return"]
            if isinstance(fn_return_type, typing.TypeVar):
                fn_return_type = Any

            # Extract state and result types from WorkflowState[S, R] | WorkflowResult[R]
            extracted_state_type, extracted_result_type = _extract_workflow_types(fn_return_type)
            if state_type is None and extracted_state_type is not None:
                fn_state_type = extracted_state_type
            if result_type is None and extracted_result_type is not None:
                fn_result_type = extracted_result_type

        if result_type is not None:
            fn_result_type = result_type
        elif fn_result_type is None:
            fn_result_type = Any

        if state_type is not None:
            fn_state_type = state_type
        elif fn_state_type is None:
            fn_state_type = Any

        fn_result_type_base = typing.cast(Any, typing.get_origin(fn_result_type) or fn_result_type)
        if not (
            isinstance(fn_result_type_base, type)
            and issubclass(fn_result_type_base, core.EntityBase)
        ):
            raise TypeError(f"Workflow functions must return fxtr entities, not {fn_result_type}")
        fn_result_type = typing.cast(Any, fn_result_type)
        fn_state_type = typing.cast(Any, fn_state_type)
        interface_spec = core._spec_of_with_mode(
            core.WorkflowFunction[fn_state_type, fn_result_type], tv_mode="as_any"
        )

        return WorkflowFunctionWrapper(
            fn=unwrapped_fn,
            module_name=unwrapped_fn.__module__,
            qualname=unwrapped_fn.__qualname__,
            requires=fn_requires,
            default_interface_spec=interface_spec,
        )

    if fn_or_none is not None:
        return decorator(fn_or_none)
    else:
        return decorator


@dataclasses.dataclass(frozen=True)
class BindImplPlaceholder[T: core.FxtrCallTarget]:
    """A placeholder for a bound implementation plan."""

    plan: core.Plan[T]


class BindImplMarker(
    core.DataEntityBase, type_id="BindImplMarker^1", namespace_id="!builtin.helpers"
):
    """A marker entity that indicates a bound implementation plan."""

    plan: core.Plan[Any]


def bind_impl[
    T: core.FxtrCallTarget
](plan: core.Plan[T],) -> T | Any:
    """Wraps a plan to indicate that it needs to be instantiated before passing it at runtime.

    This function is intended to be used in combination with `make_plan_from` or `make_task_from`
    to "tag" a particular plan as needing to be instantiated before passing it to the function
    that is wrapped by `make_plan_from` or `make_task_from`.

    Args:
        plan: The plan to wrap.

    Returns:
        A placeholder that `make_plan_from` or `make_task_from` will use to "tag" the plan so that
        it will be resolved and instantiated at runtime. (Note: for typechecking reasons, we pretend
        that this function _could_ return an actual value of type T immediately. This is just to
        allow the typechecker to verify that the result of the plan is of a type that the
        function could actually accept after being unwrapped.)
    """
    return BindImplPlaceholder(plan)


@dataclasses.dataclass(frozen=True)
class BindStoragePlaceholder[R: core.AnyEntity]:
    """A placeholder for a bound implementation plan."""

    plan: core.Plan[core.StorageHandle[R]]


class BindStorageMarker(
    core.DataEntityBase, type_id="BindStorageMarker^1", namespace_id="!builtin.helpers"
):
    """A marker entity that indicates a bound storage plan."""

    plan: core.Plan[core.StorageHandle[Any]]


def bind_storage[
    R: core.AnyEntity
](plan: core.Plan[core.StorageHandle[R]],) -> R | Any:
    """Wraps a storage handle to indicate that it needs to be retrieved before passing it at runtime.

    This function is intended to be used in combination with `make_plan_from` or `make_task_from`
    to "tag" a particular storage handle as needing to be resolved and then retrieved before passing
    it to the function that is wrapped by `make_plan_from` or `make_task_from`.

    Args:
        plan: The plan to wrap.

    Returns:
        A placeholder that `make_plan_from` or `make_task_from` will use to "tag" the plan so that
        it will be resolved and retrieved at runtime. (Note: for typechecking reasons, we pretend
        that this function _could_ return an actual value of type R immediately. This is just to
        allow the typechecker to verify that the value in the handle is of a type that the
        function could actually accept after being unwrapped.)
    """
    return BindStoragePlaceholder(plan)


async def _unwrap_markers_async(value: Any, resolver: core.PlanResolver) -> Any:
    """Recursively unwrap BindImplMarker and BindStorageMarker from a value (async version).

    Args:
        value: The value to unwrap, which can be a tuple, frozendict, or leaf value.
        resolver: The plan resolver to use for unwrapping markers.

    Returns:
        The value with markers unwrapped.
    """
    # Check for markers (these are entities, so use isinstance)
    if isinstance(value, BindImplMarker):
        return resolver(value.plan)
    if isinstance(value, BindStorageMarker):
        storage_handle = resolver(value.plan)
        return await storage_handle.retrieve()

    # Handle tuples
    if isinstance(value, tuple):
        unwrapped_items = []
        for item in value:
            unwrapped_item = await _unwrap_markers_async(item, resolver)
            unwrapped_items.append(unwrapped_item)
        return tuple(unwrapped_items)

    # Handle frozendicts
    if isinstance(value, frozendict):
        unwrapped_items = {}
        for k, v in value.items():
            unwrapped_key = await _unwrap_markers_async(k, resolver)
            unwrapped_value = await _unwrap_markers_async(v, resolver)
            unwrapped_items[unwrapped_key] = unwrapped_value
        return frozendict(unwrapped_items)

    # Leaf value, return as-is
    return value


def _replace_placeholders_with_markers(value: Any) -> tuple[Any, list[core.Plan[Any]]]:
    """Recursively replace BindImplPlaceholder and BindStoragePlaceholder with markers.

    Also checks for disallowed markers and raises an error if found.

    Args:
        value: The value to process, which can be a tuple, frozendict, or leaf value.

    Returns:
        A tuple of (processed_value, list_of_plans) where:
        - processed_value: The value with placeholders replaced by markers
        - list_of_plans: All plans encountered during replacement (from placeholders)

    Raises:
        ValueError: If BindImplMarker or BindStorageMarker are found (disallowed).
    """
    plans: list[core.Plan[Any]] = []

    # Check for disallowed markers
    if isinstance(value, BindImplMarker):
        raise ValueError(
            "BindImplMarker found in make_task_from arguments. "
            "Use BindImplPlaceholder instead, or use bind_impl() directly."
        )
    if isinstance(value, BindStorageMarker):
        raise ValueError(
            "BindStorageMarker found in make_task_from arguments. "
            "Use BindStoragePlaceholder instead, or use bind_storage() directly."
        )

    # Check for placeholders and replace with markers
    if isinstance(value, BindImplPlaceholder):
        plans.append(value.plan)
        return BindImplMarker(value.plan), plans
    if isinstance(value, BindStoragePlaceholder):
        plans.append(value.plan)
        return BindStorageMarker(value.plan), plans

    # Handle tuples
    if isinstance(value, tuple):
        processed_items = []
        for item in value:
            processed_item, item_plans = _replace_placeholders_with_markers(item)
            processed_items.append(processed_item)
            plans.extend(item_plans)
        return tuple(processed_items), plans

    # Handle frozendicts
    if isinstance(value, frozendict):
        processed_items = {}
        for k, v in value.items():
            processed_key, key_plans = _replace_placeholders_with_markers(k)
            processed_value, value_plans = _replace_placeholders_with_markers(v)
            processed_items[processed_key] = processed_value
            plans.extend(key_plans)
            plans.extend(value_plans)
        return frozendict(processed_items), plans

    # Leaf value, return as-is
    return value, plans


class _UnbindingTaskFunctionImpl(core.TaskFunction[Any]):
    """A task function that unbinds the arguments to the function."""

    def __init__(
        self,
        resolver: core.PlanResolver,
        /,
        fn_entrypoint: str,
        interface_spec: core.InterfaceSpec[core.TaskFunction[Any]],
        fn_kwargs: frozendict[str, Any],
    ):
        self._resolver = resolver
        self._fn_entrypoint = fn_entrypoint
        self._interface_spec = interface_spec
        self._fn_kwargs = fn_kwargs

    def interface_spec(self) -> core.InterfaceSpec[core.TaskFunction[Any]]:
        return self._interface_spec

    async def run_task(self) -> Any:
        # Walk the fn_kwargs, assuming they are either tuples, frozendicts, or leaf values,
        # and look for BindImplMarker and BindStorageMarker. If found:
        # - BindImplMarker: unwrap using `resolver(plan)`
        # - BindStorageMarker: unwrap using `await resolver(plan).retrieve()`
        unwrapped_kwargs = await _unwrap_markers_async(self._fn_kwargs, self._resolver)

        # Load the function from the entrypoint
        fn = in_process_py_provider._load_object_from_entrypoint(self._fn_entrypoint)

        # Call the function with the unwrapped kwargs.
        # The function can be either:
        # - A sync function returning R
        # - An async function returning Awaitable[R] (or Coroutine[Any, Any, R])
        result = fn(**unwrapped_kwargs)

        # If the function returned a coroutine/awaitable, await it
        if inspect.iscoroutine(result):
            result = await result

        return result


def make_task_from[
    R: core.AnyEntity, **P
](
    fn: Callable[P, R] | Callable[P, Awaitable[R]] | str,
    requires: str | None = None,
    return_type: type[R] | None = None,
) -> Callable[P, core.Task[R]]:
    """Create a task from a function.

    The function can be either:
    - A synchronous function that returns R directly
    - An asynchronous function that returns Awaitable[R] (or Coroutine[Any, Any, R])

    Args:
        fn: The function to create a task from. Can be a sync function returning R, an async
            function returning Awaitable[R], or a string entrypoint.
        requires: The requirements string for the task. If not provided, will be inferred from
            the module of the function.
        return_type: The type of the return value of the task. If not provided, will be inferred
            from the return type of the function.

    Returns: A new callable, which takes the same arguments as the function and returns a task.
        This new callable can be used to create a task that will run the function. Optionally, the
        arguments to the new callable can be placeholders built by `bind_impl` and `bind_storage`.
    """
    if requires is None:
        if isinstance(fn, str):
            raise ValueError("Requires string must be provided if fn is a string")
        requires = _infer_requires_from_module(fn.__module__)

    if isinstance(fn, str):
        sig = None
        if return_type is None:
            raise ValueError(
                "Return type must be provided if fn is a string. Provide 'Any' if the return type"
                " is not known."
            )
    else:
        sig = inspect.signature(fn, eval_str=True)
        # Validate the signature the same way as build_plan does, but WITHOUT the resolver
        # synthetic parameter.
        for param in list(sig.parameters.values()):
            if (
                param.kind == inspect.Parameter.POSITIONAL_ONLY
                or param.kind == inspect.Parameter.VAR_POSITIONAL
            ):
                raise ValueError(
                    "Positional-only or var-positional parameters are not supported in"
                    " make_task_from function signatures; all arguments must be passable by"
                    " keyword!"
                )

        # Extract the return type from the signature.
        if return_type is None:
            if sig.return_annotation is not inspect.Signature.empty:
                return_type = sig.return_annotation
            else:
                raise ValueError(
                    "Return type must be provided if function has no return type annotation. "
                    "Provide 'Any' if the return type is not known."
                )

    if not isinstance(fn, str):
        if not callable(fn):
            raise ValueError("fn must be a callable or a string")

        # Entrypoint consistency check (same as with_constructor)
        unwrapped_fn = inspect.unwrap(fn)
        mod = getattr(unwrapped_fn, "__module__", None)
        qualname = getattr(unwrapped_fn, "__qualname__", None)
        if mod is None or qualname is None or ("<locals>" in qualname):
            raise ValueError(
                "Callable functions must be exposed functions or classes (having"
                " `__module__` and `__qualname__` attributes) in order to be converted into a"
                " task!"
            )
        if mod == "__main__":
            raise ValueError(
                "Cannot capture a function from the main module; please expose it as a"
                " function or class in a separate importable module!"
            )
        fn_str = f"{mod}:{qualname}"
        ep = in_process_py_provider._load_object_from_entrypoint(fn_str)
        if ep != fn:
            raise ValueError(
                f"Function {fn} advertises itself as being available at entrypoint"
                f" string {fn_str}, but it does not match the object {ep} that is actually"
                " present there!"
            )
        fn = fn_str

    # At this point, return_type should never be None
    assert return_type is not None, "return_type must be set by this point"

    def _bind(*args: P.args, **kwargs: P.kwargs) -> core.Task[R]:
        if sig is None:
            if args:
                raise ValueError("Cannot bind arguments to a task without a signature")
            bound_kwargs = frozendict(kwargs)
        else:
            bound = sig.bind(*args, **kwargs)
            bound_kwargs = frozendict(bound.arguments)

        # Walk the bound kwargs, assuming they are either tuples, frozendicts, or leaf values,
        # and look for:
        # - BindImplPlaceholder
        # - BindStoragePlaceholder
        # If found, replace the placeholder with an appropriate marker.
        # Also look for BindImplMarker and BindStorageMarker, and error if they are found; they
        # are disallowed by this helper.
        processed_kwargs, encountered_plans = _replace_placeholders_with_markers(bound_kwargs)

        spec = core.spec_of(core.TaskFunction[return_type])
        plan = (
            build_plan(
                _UnbindingTaskFunctionImpl,
                requires=requires,
                interface_spec=spec,
            )
            .with_runtime_deps(encountered_plans)
            .bind(fn_entrypoint=fn, interface_spec=spec, fn_kwargs=processed_kwargs)
        )
        return core.Task(function=plan)

    return _bind


async def _unbinding_plan_ctor(
    resolver: core.PlanResolver,
    /,
    ctor_entrypoint: str,
    ctor_kwargs: frozendict[str, Any],
) -> core.FxtrCallTarget:
    """Constructor for a plan that unbinds markers and calls a function.

    Args:
        resolver: The plan resolver to use for unwrapping markers.
        ctor_entrypoint: The entrypoint string for the function to call.
        ctor_kwargs: The keyword arguments to pass to the function, which may contain markers.

    Returns:
        The result of calling the function, which should be a FxtrCallTarget.
    """
    # Walk the ctor_kwargs, assuming they are either tuples, frozendicts, or leaf values,
    # and look for BindImplMarker and BindStorageMarker. If found:
    # - BindImplMarker: unwrap using `resolver(plan)`
    # - BindStorageMarker: unwrap using `await resolver(plan).retrieve()`
    unwrapped_kwargs = await _unwrap_markers_async(ctor_kwargs, resolver)

    # Load the function from the entrypoint
    fn = in_process_py_provider._load_object_from_entrypoint(ctor_entrypoint)

    # Call the function with the unwrapped kwargs.
    # The function can be either:
    # - A sync function returning T
    # - An async function returning Awaitable[T] (or Coroutine[Any, Any, T])
    result = fn(**unwrapped_kwargs)

    # If the function returned a coroutine/awaitable, await it
    if inspect.iscoroutine(result):
        result = await result

    return result


def make_plan_from[
    T: core.FxtrCallTarget, **P
](
    fn: Callable[P, T] | Callable[P, Awaitable[T]] | str,
    requires: str | None = None,
    return_type: type[T] | None = None,
) -> Callable[P, core.Plan[T]]:
    """Create a plan from a function.

    The function can be either:
    - A synchronous function that returns T directly, or the class constructor for a class that
      implements T
    - An asynchronous function that returns Awaitable[T] (or Coroutine[Any, Any, T])


    Args:
        fn: The function to create a plan from. Can be a sync callable returning T, an async
            function returning Awaitable[T], or a string entrypoint.
        requires: The requirements string for the plan. If not provided, will be inferred from
            the module of the function.
        return_type: The type of the return value of the plan. If not provided, will be inferred
            from the return type of the function.

    Returns: A new callable, which takes the same arguments as the function and returns a plan.
        This new callable can be used to create a plan that will run the function. Optionally, the
        arguments to the new callable can be placeholders built by `bind_impl` and `bind_storage`.
    """
    if requires is None:
        if isinstance(fn, str):
            raise ValueError("Requires string must be provided if fn is a string")
        requires = _infer_requires_from_module(fn.__module__)

    if isinstance(fn, str):
        sig = None
        if return_type is None:
            raise ValueError(
                "Return type must be provided if fn is a string. Provide 'Any' if the return type"
                " is not known."
            )
    else:
        sig = inspect.signature(fn, eval_str=True)
        # Validate the signature the same way as build_plan does, but WITHOUT the resolver
        # synthetic parameter.
        for param in list(sig.parameters.values()):
            if (
                param.kind == inspect.Parameter.POSITIONAL_ONLY
                or param.kind == inspect.Parameter.VAR_POSITIONAL
            ):
                raise ValueError(
                    "Positional-only or var-positional parameters are not supported in"
                    " make_plan_from function signatures; all arguments must be passable by"
                    " keyword!"
                )

        # Extract the return type from the signature.
        if return_type is None:
            if inspect.isclass(fn):
                # If fn is a class, use the class itself as the return type
                return_type = fn
            elif sig.return_annotation is not inspect.Signature.empty:
                return_type = sig.return_annotation
            else:
                raise ValueError(
                    "Return type must be provided if function has no return type annotation. "
                    "Provide 'Any' if the return type is not known."
                )

    if not isinstance(fn, str):
        if not callable(fn):
            raise ValueError("fn must be a callable or a string")

        # Entrypoint consistency check (same as with_constructor)
        unwrapped_fn = inspect.unwrap(fn)
        mod = getattr(unwrapped_fn, "__module__", None)
        qualname = getattr(unwrapped_fn, "__qualname__", None)
        if mod is None or qualname is None or ("<locals>" in qualname):
            raise ValueError(
                "Callable functions must be exposed functions or classes (having"
                " `__module__` and `__qualname__` attributes) in order to be converted into a"
                " plan!"
            )
        if mod == "__main__":
            raise ValueError(
                "Cannot capture a function from the main module; please expose it as a"
                " function or class in a separate importable module!"
            )
        fn_str = f"{mod}:{qualname}"
        ep = in_process_py_provider._load_object_from_entrypoint(fn_str)
        if ep != fn:
            raise ValueError(
                f"Function {fn} advertises itself as being available at entrypoint"
                f" string {fn_str}, but it does not match the object {ep} that is actually"
                " present there!"
            )
        fn = fn_str

    # At this point, return_type should never be None
    assert return_type is not None, "return_type must be set by this point"

    # Infer interface spec using spec_of_concrete_target (same as with_constructor)
    # Note: return_type is already set to fn if fn is a class, or to the return annotation
    try:
        interface_spec = core.spec_of_concrete_target(return_type)
    except Exception as e:
        e.add_note(f"While inferring interface spec of plan from function {fn}")
        raise

    # Check that the interface is stateless (same as with_constructor)
    if not interface_spec.defn.stateless:
        raise ValueError(
            f"Interface definition {interface_spec.defn.interface_id} is not stateless! "
            "Stateful interfaces cannot be directly instantiated"
            " via fxtr plans; please instead use the plan to construct an immutable 'factory'"
            " call target that dynamically constructs mutable instances."
        )

    def _bind(*args: P.args, **kwargs: P.kwargs) -> core.Plan[T]:
        if sig is None:
            if args:
                raise ValueError("Cannot bind arguments to a plan without a signature")
            bound_kwargs = frozendict(kwargs)
        else:
            bound = sig.bind(*args, **kwargs)
            bound_kwargs = frozendict(bound.arguments)

        # Walk the bound kwargs, assuming they are either tuples, frozendicts, or leaf values,
        # and look for:
        # - BindImplPlaceholder
        # - BindStoragePlaceholder
        # If found, replace the placeholder with an appropriate marker.
        # Also look for BindImplMarker and BindStorageMarker, and error if they are found; they
        # are disallowed by this helper.
        processed_kwargs, encountered_plans = _replace_placeholders_with_markers(bound_kwargs)

        plan = (
            build_plan(
                _unbinding_plan_ctor,
                requires=requires,
                interface_spec=interface_spec,
            )
            .with_runtime_deps(encountered_plans)
            .bind(ctor_entrypoint=fn, ctor_kwargs=processed_kwargs)
        )
        return plan

    return _bind
