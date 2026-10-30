"""Implementation plan and associated types for Python objects."""

from __future__ import annotations

from typing import Any

from frozendict import frozendict

from ..object_model import core


class PyImplPlan[T: core.FxtrCallTarget](
    core.ConfiguredPlan[T],
    core.DataEntityBase,
    namespace_id="!builtin.protocol_impl",
    type_id="PyImplPlan^1",
):
    """A plan for materializing a Python object.

    The constructor is specified as an entry-point-style string (e.g., "package.module:function")
    that resolves to a callable with the following signature:

    ```python
    def some_builder(
        resolver: core.PlanResolver,
        /,
        **kwargs: core.AnyFrozenIPLDable,
    ) -> FxtrCallTarget:
        ...
    ```
    The first argument (always passed positionally) will be a resolver function that can resolve
    plan dependencies to their implementations. The remaining arguments will be the keyword arguments
    passed to this constructor by the plan. The constructor should return a FxtrCallTarget object
    that implements the protocol specified by this plan.

    The constructor must be a deterministic function of its arguments. Fxtr assumes it can safely
    call the constructor multiple times to construct an equivalent call target, and it may also
    share a single instance of the call target between multiple tasks.

    Together with the requires string, the constructor string should uniquely identify the
    implementation. This means that the implementation should have the same observable behavior
    across any package version of the package that satisfies the requires string. As such, changes
    to the implementation provided by a constructor should be considered breaking changes. In
    general, this means that either (a) the constructor string should refer to a public API that the
    package author has guaranteed to be stable across versions, or (b) the requires string should
    pin a specific version with known behavior.

    Attributes:
        interface_spec: The protocol that this implementation plan implements.
        requires: The requirement for the Python object. Usually a single package name and version
            in PEP 440 format, but can also be a newline-separated list of requirements.
        constructor: Entry-point-style string (e.g., "package.module:function") that resolves to a
            callable that can be called with the given kwargs, prefixed with a single positional
            argument that is a plan resolver. When called, the constructor must
            return an object that is an instance of `FxtrCallTarget`, or an awaitable that produces
            such an object. WARNING: this is unstable and subject to change.
        kwargs: The keyword arguments to pass to the constructor. These will be directly passed to
            the constructor without modification.
        impl_deps: The implementation dependencies of this plan. These are the set of implementation
            plans that this plan will need to instantiate at runtime. The provider for this plan
            will not be able to instantiate any implementation plans that are not provided in
            `impl_deps`, even if those plans are passed as kwargs.
    """

    interface_spec: core.InterfaceSpec[T]
    requires: str
    constructor: str
    kwargs: frozendict[str, core.AnyFrozenIPLDable]
    impl_deps: frozenset[core.Plan[Any]]
