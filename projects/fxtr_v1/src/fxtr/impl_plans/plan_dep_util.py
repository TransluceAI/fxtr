"""Mapping of plan objects to their dependencies."""

from typing import Any, Collection

from ..object_model import core
from . import py_impl_plan


def plan_deps(
    plan: core.Plan[Any],
    recursive: bool = False,
) -> Collection[core.Plan[Any]]:
    """Get the dependencies of a plan.

    Each plan needs to know what other plans must be available at runtime in order for the
    plan to be runnable. Then, at runtime, the provider of the plan will take as input the
    implementations of those dependencies, in order to implement the plan itself.

    In addition to walking the dependencies of plans in order to instantiate them, we can also
    walk them in order to inspect task ordering. This is because every task has a corresponding
    output handle, which is a plan representing how to retrieve the results of that task when they
    are available.

    Note that the dependencies returned by this function are the _fxtr-internal plans_ for services
    that must be provided to the implementation of the plan in order to instantiate it. This does
    NOT include external state like library dependencies or dependencies on external storage; those
    are the responsibility of the provider/implementation of the plan.

    Args:
        plan: The root plan to get dependencies of. All plans should be registered with this
            function.
        recursive: If True, return all transitive dependencies. If False, return only direct
            dependencies.

    Returns:
        Set of all plans this plan depends on (direct or transitive based on recursive parameter).
    """
    if recursive:
        return _plan_deps_recursive(plan)
    else:
        return _plan_deps_direct(plan)


def _plan_deps_direct(plan: core.Plan[Any]) -> frozenset[core.Plan[Any]]:
    """Get the direct (non-recursive) dependencies of a plan."""
    match plan:
        case (
            core.TaskResultHandle() | core.WorkflowStepResultHandle() | core.WorkflowResultHandle()
        ):
            # Task result handles themselves don't have dependencies; they point "outside" the
            # fxtr system and must be provided directly by the tooling.
            return frozenset()
        case core.ExternalServiceIdentifier():
            # External service identifiers also don't have dependencies; they point "outside"
            # the fxtr system and must be provided directly by the tooling.
            return frozenset()
        case core.ConstantHandle():
            # Constant handles don't have dependencies; they point to a constant value.
            return frozenset()
        case py_impl_plan.PyImplPlan(impl_deps=impl_deps):
            # Python implementations are custom code which operates in a sandbox. Any non-sandbox
            # dependencies they need access to must be provided externally; their `impl_deps`
            # attribute tracks this.
            return frozenset(impl_deps)
        case _:
            raise ValueError(f"Unrecognized plan type: {type(plan)}")


def _plan_deps_recursive(plan: core.Plan[Any]) -> frozenset[core.Plan[Any]]:
    """Get all transitive dependencies of a plan using iterative traversal."""
    stack = [plan]
    visited = set()

    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)

        # Get direct dependencies of current plan
        direct_deps = _plan_deps_direct(current)

        # Add direct dependencies to stack for further traversal
        for dep in direct_deps:
            if dep not in visited:
                stack.append(dep)

    return frozenset(visited)
