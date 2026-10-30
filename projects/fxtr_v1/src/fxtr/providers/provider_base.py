"""Base class for providers.

Providers are responsible for providing implementations of protocol implementation plans. Each
provider generally knows how to provide a subset of protocol implementation plans.

Providers are used by resolvers to provide implementations of protocol implementation plans.
"""

import abc
import dataclasses
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..object_model import core


class ProviderUnsupportedPlanError(Exception):
    """An error raised when a provider does not support a given plan.

    This error should be raised when a provider does not support a given plan, and a different
    provider should be used instead.
    """


class ProviderUnsupportedProvenanceLockError(Exception):
    """An error raised when a provider does not support a given provenance.

    This error should be raised when a provider does not support a given type of provenance lock,
    and a different provider should be used instead.
    """


class InvalidPlanError(Exception):
    """An error raised when a plan is invalid and cannot be materialized.

    This error can be raised to indicate that a plan object does not correctly specify how to
    materialize the requested object. For instance, if the plan references a particular package
    requirement and a specific entrypoint, but there is a version of the package that satisfies the
    requirement but does not have that entrypoint, it would be appropriate to raise this error.

    (Note that if the error is internal to the provider or to this user's environment, the provider
    should NOT raise this error. For instance, if the user tries to instantiate a plan in a specific
    environment that does not meet the plan's requirements, this is an error with the _user_'s
    environment, not with the plan, so the provider should raise a different error instead, such as
    RuntimeError.)
    """


class ImplProvider(abc.ABC):
    """Provider that provides implementations of protocol implementation plans."""

    async def prepare(
        self,
        plan_provenance_pairs: Sequence[tuple[core.Plan[Any], core.AnyEntity | None]],
    ) -> None:
        """Prepare plans by pre-computing environments or validating dependencies.

        This method allows providers to batch-prepare multiple plans before they are needed.
        The default implementation does nothing, which is appropriate for providers that
        can instantiate plans on-demand without preparation overhead.

        Args:
            plan_provenance_pairs: Sequence of (plan, provenance) pairs to prepare.
        """
        pass  # Default no-op implementation

    @abc.abstractmethod
    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        """Provide implementation of a single protocol implementation plan.

        This method can be used by a resolver to provide concrete implementation of a
        protocol implementation plan.

        Providers are not required to provide all types of plan; the resolver is responsible for
        routing plans to an appropriate provider.

        Args:
            plan: The protocol implementation plan to provide implementation for.
            dependencies: A mapping of plan dependencies to their instantiated
                implementations, potentially provided by a different provider.
            provenance_lock: Optional provenance entity from a previous instantiation of this plan
                to use when providing this plan. If provided, this provider MUST ensure that
                the same version of any dependencies that were used in the previous instantiation
                are used in the current instantiation, or raise an error. If not provided, the
                provider is free to provide the plan in any way.

        Returns:
            A tuple containing the implementation and the provenance for the implementation.
            Provenance entities MUST uniquely identify this provider and SHOULD identify all
            dependencies used to provide the implementation.
        """
        raise NotImplementedError()


@dataclasses.dataclass(frozen=True)
class SimpleCallableProvider(ImplProvider):
    """Provider that just calls a callable."""

    provide_fn: Callable[
        [
            core.Plan[Any],
            Mapping[core.Plan[Any], core.FxtrCallTarget],
            core.AnyEntity | None,
        ],
        Awaitable[tuple[core.FxtrCallTarget, core.AnyEntity]],
    ]

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        return await self.provide_fn(plan, dependencies, provenance_lock)
