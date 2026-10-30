"""Provider that provides implementations of Python implementation plans in the current process."""

import functools
import importlib
import importlib.metadata
import inspect
import logging
from typing import Any, Mapping

import packaging.requirements
import packaging.version
from frozendict import frozendict

from ..impl_plans.py_impl_plan import PyImplPlan
from ..object_model import core, locator
from . import provider_base

logger = logging.getLogger(__name__)


class InProcessPyImplProvenance(
    core.DataEntityBase, namespace_id="!builtin.provenance", type_id="InProcessPyImplProvenance^1"
):
    """Provenance for a local in-process Python implementation.

    Local provenance info is provided on a best-effort basis, since we cannot guarantee that the
    environment in which the local implementation was instantiated is reproducible.

    Attributes:
        package_versions: A dictionary of package names from the `requires` field of the PyImplPlan
            to the versions that were used to instantiate the local implementation. Transitive
            dependencies may or may not be included.
    """

    package_versions: frozendict[str, str]


class InProcessPyProvider(provider_base.ImplProvider):
    """Provider that provides implementations of Python implementation plans in the current process.

    InProcessPyProvider only supports PyImplPlan objects, and also only supports implementations
    that are compatible with the current Python environment.
    """

    def __init__(self, known_loader: locator.CachingEntityLoader | None = None):
        self._known_loader = known_loader or locator.CachingEntityLoader.empty()

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        """Provide implementation of a single protocol implementation plan.

        Args:
            plan: The implementation plan to provide.
            dependencies: A mapping of plan dependencies to their instantiated implementations.
            provenance_lock: Optional provenance entity from a previous instantiation to use when
                providing this implementation.

        Returns:
            A tuple containing the implementation and the provenance for the implementation.
        """

        if not isinstance(plan, PyImplPlan):
            raise provider_base.ProviderUnsupportedPlanError(
                f"InProcessPyProvider only supports PyImplPlan objects, got {type(plan)}"
            )

        if provenance_lock is not None and not isinstance(
            provenance_lock, InProcessPyImplProvenance
        ):
            raise provider_base.ProviderUnsupportedProvenanceLockError(
                "InProcessPyProvider only supports InProcessPyImplProvenance for provenance"
                f" locking, got {type(provenance_lock)}"
            )

        # Look up or verify the provenance
        all_deps = _walk_requirements_tree(
            tuple(packaging.requirements.Requirement(req).name for req in plan.requires.split("\n"))
        )
        local_provenance = InProcessPyImplProvenance(package_versions=frozendict(all_deps))

        if provenance_lock is not None:
            if provenance_lock != local_provenance:
                lock_only = {
                    k: v
                    for k, v in provenance_lock.package_versions.items()
                    if v != local_provenance.package_versions.get(k)
                }
                here_only = {
                    k: v
                    for k, v in local_provenance.package_versions.items()
                    if v != provenance_lock.package_versions.get(k)
                }
                raise provider_base.InvalidPlanError(
                    "Error materializing a PyImplPlan locally: Requested provenance lock does not"
                    " match dependencies in the current environment. Differing dependencies:\n"
                    f"Locked: {lock_only}\nIn current env: {here_only}"
                )

        impl = await self.provide_one_no_provenance(plan, dependencies)
        return impl, local_provenance

    async def provide_one_no_provenance(
        self,
        plan: PyImplPlan,
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
    ) -> core.FxtrCallTarget:
        """Provide an implementation of a Python implementation plan, without checking provenance.

        This method can be used in settings where we already know the provenance is correct, and
        want to directly instantiate the implementation. In particular, this can be used by other
        providers to instantiate implementations of Python implementation plans.

        Args:
            plan: The Python implementation plan to provide an implementation for.
            dependencies: A mapping of plan dependencies to previously-instantiated implementations,
                potentially provided by a different provider.

        Returns:
            The implementation.
        """
        if not plan.interface_spec.defn.stateless:
            raise provider_base.InvalidPlanError(
                "Error materializing a PyImplPlan: Only stateless interfaces can be directly"
                " instantiated via fxtr plans"
            )

        # Parse the package requirements
        root_distns = []
        for req in plan.requires.split("\n"):
            try:
                requirement = packaging.requirements.Requirement(req)
            except packaging.requirements.InvalidRequirement as e:
                raise provider_base.InvalidPlanError(
                    f"Error materializing a PyImplPlan: Invalid package requirement '{req}': {e}"
                ) from e

            # Get the distribution for the specific package
            try:
                distribution = importlib.metadata.distribution(requirement.name)
            except importlib.metadata.PackageNotFoundError as e:
                raise RuntimeError(
                    f"Error materializing a PyImplPlan: Package '{requirement.name}' is not"
                    " installed"
                ) from e

            root_distns.append(distribution)

            # Check if the installed version satisfies the requirement
            installed_version = packaging.version.parse(distribution.version)
            if not requirement.specifier.contains(installed_version, prereleases=True):
                raise RuntimeError(
                    f"Error materializing a PyImplPlan: Installed package '{requirement.name}'"
                    f" version {installed_version} does not satisfy requirement"
                    f" {requirement.specifier}"
                )

        if not root_distns:
            raise provider_base.InvalidPlanError(
                "Error materializing a PyImplPlan: No packages found that satisfy the requirement"
                f" {plan.requires}"
            )

        # Load the builder function from the builder string
        try:
            builder_func = _load_object_from_entrypoint(plan.constructor)
        except (ValueError, ImportError, AttributeError) as e:
            raise provider_base.InvalidPlanError(
                "Error materializing a PyImplPlan: Failed to load builder"
                f" '{plan.constructor}': {e}"
            ) from e

        # Call the builder with the given kwargs
        def resolver(dep: core.Plan[Any]) -> core.FxtrCallTarget:
            return dependencies[dep]

        retval = builder_func(resolver, **plan.kwargs)
        if inspect.isawaitable(retval):
            retval = await retval

        # Check if it is a FxtrCallTarget
        if isinstance(retval, core.FxtrCallTarget):
            impl = retval
        else:
            raise provider_base.InvalidPlanError(
                f"Error materializing a PyImplPlan: Constructor '{plan.constructor}' did not return"
                " a FxtrCallTarget"
            )

        try:
            core.implements_spec(
                impl, plan.interface_spec, allow_untyped_impl=True, error_if_not=True
            )
        except core.ReifiedTypeCheckError as e:
            raise provider_base.InvalidPlanError(
                f"Error materializing a PyImplPlan: Constructor '{plan.constructor}' returned an"
                " implementation of the wrong call target interface."
            ) from e
        return impl


def _load_object_from_entrypoint(entrypoint: str) -> Any:
    """Load a Python object from an entry-point-style string.

    The entry-point string should be in the format "module.path:attr.path" where:
    - "module.path" is the module to import
    - "attr.path" is a dotted attribute path within the module (optional)

    Nested attributes like "my.module:Class.method" are supported.

    Args:
        entrypoint: Entry-point-style string (e.g., "package.module:Class.method")

    Returns:
        The loaded object

    Raises:
        ValueError: If the entrypoint format is invalid
        ImportError: If the module cannot be imported
        AttributeError: If the attribute path cannot be resolved
    """
    # Split on ':' to separate module from attribute path
    if ":" in entrypoint:
        module_name, attr_path = entrypoint.split(":", 1)
    else:
        raise ValueError(
            f"Constructor string must be in format 'module.path:attribute', got '{entrypoint}'"
        )

    # Import the module
    module = importlib.import_module(module_name)

    # Get the target object by walking the attribute path
    target = module
    for attr_name in attr_path.split("."):
        target = getattr(target, attr_name)

    return target


@functools.cache
def _walk_requirements_tree(root_names: tuple[str, ...]) -> dict[str, str]:
    """Walk the requirements tree for a package.

    Args:
        root_names: The names of the packages to walk the requirements tree for.

    Returns:
        A map from package names to versions.
    """
    logger.info(f"Walking requirements tree for %s...", root_names)
    results = {}
    seen = set()
    pending = list(root_names)
    while pending:
        package_name = pending.pop()
        try:
            distribution = importlib.metadata.distribution(package_name)
        except importlib.metadata.PackageNotFoundError:
            continue
        results[package_name] = distribution.version
        if distribution.requires is None:
            continue
        for requirement in distribution.requires:
            new_name = packaging.requirements.Requirement(requirement).name
            if new_name not in seen:
                seen.add(new_name)
                pending.append(new_name)
    logger.info(f"Done walking requirements tree for %s", root_names)
    return results
