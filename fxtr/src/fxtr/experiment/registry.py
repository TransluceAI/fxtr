"""Where steps and workflows are found by name.

A runner looks the functions it runs up by name: a run node names the step it calls, an invocation
names its workflow, and on a resume nothing but the name is left. A :class:`FunctionRegistry` is
that lookup. There is one registry a process fills by importing: the decorators
:func:`~fxtr.experiment.steps.step` and :func:`~fxtr.experiment.workflows.workflow` add every
function they build to :data:`GLOBAL_REGISTRY`, so importing the module that defines a function is
what makes it runnable, and a process that must run a job it did not launch imports the project's
modules and has what it needs. A runner uses the global registry unless it is handed another;
tests build their own from a list of functions.

**Names.** A function's name is its identity to the runner and in every record: cached results
match by step name, invocations are found by workflow name. The decorators default it to the
body's fully-qualified ``module.qualname``, which is stable as long as the module is imported
under one name. A function defined in ``__main__`` has no stable qualified name, since the same
file imported as a module would give another, so the decorators require ``name=`` there.

**Duplicates.** Importing many modules may register several functions under one name (a test
session's many small ``sample`` steps, say). Registration never fails: the registry keeps every
registration per name, and only looking an *ambiguous* name up raises, naming every module that
defined one. Registering the same object twice is a no-op.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fxtr.experiment.steps import Step
    from fxtr.experiment.workflows import Workflow

type Function = Step[Any, Any, Any] | Workflow[Any, Any, Any]
"""A function a runner can run (evaluated lazily, so the classes need not be imported here)."""


def default_name(body: Any) -> str:
    """The name a decorator gives a function that was not given one: the body's fully-qualified
    ``module.qualname``.

    Raises:
        TypeError: If the body is defined in ``__main__``, whose qualified names are not stable
            (see the module docstring); pass ``name=`` instead.
    """
    if body.__module__ == "__main__":
        raise TypeError(
            f"{body.__qualname__} is defined in __main__, so its qualified name would differ when"
            " the file is imported as a module. Give it an explicit name= or define it in a"
            " module."
        )
    return f"{body.__module__}.{body.__qualname__}"


class RegistryError(Exception):
    """A name is not registered, is registered by more than one function, or is registered by the
    wrong kind of function."""


class FunctionRegistry:
    """The steps and workflows a runner can run, by name (one shared namespace).

    Args:
        functions: Functions to register up front.
    """

    def __init__(self, functions: Iterable[Function] = ()) -> None:
        self._by_name: dict[str, list[Function]] = {}
        for fn in functions:
            self.add(fn)

    def add(self, fn: Function) -> None:
        """Register a function under its name. Adding the same object again does nothing; adding
        a different function under a name already taken makes the name ambiguous, which is an
        error only when it is looked up."""
        registered = self._by_name.setdefault(fn.name, [])
        if not any(existing is fn for existing in registered):
            registered.append(fn)

    @property
    def functions(self) -> Collection[Function]:
        """Every registered function, in registration order.

        Raises:
            RegistryError: If any name is ambiguous.
        """
        return tuple(self._lookup(name) for name in self._by_name)

    def unambiguous(self) -> dict[str, Function]:
        """Every name registered by exactly one function, with that function: the names a lookup
        succeeds for. Never raises."""
        return {name: fns[0] for name, fns in self._by_name.items() if len(fns) == 1}

    def function(self, name: str) -> Function:
        """The step or workflow called ``name``.

        Raises:
            RegistryError: If nothing or more than one function is registered there.
        """
        return self._lookup(name)

    def step(self, name: str) -> Step[Any, Any, Any]:
        """The step called ``name``.

        Raises:
            RegistryError: If nothing, more than one function, or a workflow is registered there.
        """
        from fxtr.experiment.steps import Step  # the decorators import this module

        fn = self._lookup(name)
        if not isinstance(fn, Step):
            raise RegistryError(f"{name!r} is a workflow, but a step was asked for")
        return fn

    def workflow(self, name: str) -> Workflow[Any, Any, Any]:
        """The workflow called ``name``.

        Raises:
            RegistryError: If nothing, more than one function, or a step is registered there.
        """
        from fxtr.experiment.workflows import Workflow  # the decorators import this module

        fn = self._lookup(name)
        if not isinstance(fn, Workflow):
            raise RegistryError(f"{name!r} is a step, but a workflow was asked for")
        return fn

    def _lookup(self, name: str) -> Function:
        registered = self._by_name.get(name, ())
        if len(registered) == 1:
            return registered[0]
        if not registered:
            raise RegistryError(
                f"No function named {name!r} is registered. Importing the module that defines it"
                " registers it; a project's [tool.fxtr] modules setting names the modules a"
                " resume imports."
            )
        where = ", ".join(f"{fn.body.__module__}.{fn.body.__qualname__}" for fn in registered)
        raise RegistryError(
            f"{name!r} is registered by {len(registered)} functions ({where}); give each a"
            " distinct name= or run with a registry holding only one of them"
        )


GLOBAL_REGISTRY = FunctionRegistry()
"""The registry the decorators fill at import time, and the runner's default."""


__all__ = ["GLOBAL_REGISTRY", "Function", "FunctionRegistry", "RegistryError", "default_name"]
