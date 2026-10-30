"""Provider that provides implementations of external services and run data."""

import dataclasses
import os
import typing
from pathlib import Path
from typing import Any, Mapping

import cbrrr
import dotenv

from ..api import api_lib, fxtrconfig
from ..object_model import core, locator
from . import provider_base


class SecretDeclarationProvenance(
    core.DataEntityBase,
    namespace_id="!builtin.provenance",
    type_id="SecretDeclarationProvenance^1",
):
    """Provenance for secrets from declarations.

    Attributes:
        declaration_cid: The CID of the SecretFromEnvVarDeclaration or
                        SecretFromOptionalEnvVarDeclaration that was used.
        env_var: The environment variable name that was read.
    """

    declaration_cid: cbrrr.CID
    env_var: str


class RunProvenance(
    core.DataEntityBase, namespace_id="!builtin.provenance", type_id="RunProvenance^1"
):
    """Provenance for run data.

    Attributes:
        run_cid: The CID of the run data entity.
    """

    run_cid: core.CID_Of[core.Run]


class WorkflowResultProvenance(
    core.DataEntityBase, namespace_id="!builtin.provenance", type_id="WorkflowResultProvenance^1"
):
    """Provenance for workflow result data.

    Attributes:
        step_run_cids: The CIDs of the run data for each step of the workflow. The last run must
            produce the final WorkflowResult, and for each previous step, `step_run_cids[i]` must
            produce a WorkflowState such that `step_run_cids[i+1]` is a run of `step_run_cids[i]`'s
            "next_step" attribute.
    """

    step_run_cids: tuple[core.CID_Of[core.Run], ...]


@dataclasses.dataclass(frozen=True)
class SecretRetrieverImpl(core.SecretRetriever[Any]):
    """Implementation of SecretRetriever that returns a configured secret value.

    Returns the appropriate interface spec based on whether the secret is optional:
    - SecretRetriever[str] if is_optional is False
    - SecretRetriever[str | None] if is_optional is True
    """

    _value: Any
    _is_optional: bool

    def interface_spec(self) -> core.InterfaceSpec:
        """Return interface spec based on whether the secret is optional."""
        if self._is_optional:
            return core.spec_of(core.SecretRetriever[str | None])
        else:
            return core.spec_of(core.SecretRetriever[str])

    async def get_secret(self) -> Any:
        return self._value


@dataclasses.dataclass(frozen=True)
class RunDataRetriever[R: core.EntityBase](core.StorageHandle[R]):
    """Implementation of a run data retriever."""

    _run_data_cid: cbrrr.CID
    _locator: locator.CachingEntityLoader
    _interface_spec: core.InterfaceSpec[core.StorageHandle[R]]

    def interface_spec(self) -> core.InterfaceSpec[core.StorageHandle[R]]:
        return self._interface_spec

    async def retrieve(self) -> R:
        return typing.cast(R, self._locator(self._run_data_cid))


@dataclasses.dataclass(frozen=True)
class WorkflowResultRetriever[R: core.EntityBase](core.StorageHandle[R]):
    """Implementation of a run data retriever."""

    _run_data_cid: cbrrr.CID
    _locator: locator.CachingEntityLoader
    _interface_spec: core.InterfaceSpec[core.StorageHandle[R]]

    def interface_spec(self) -> core.InterfaceSpec[core.StorageHandle[R]]:
        return self._interface_spec

    async def retrieve(self) -> R:
        res = self._locator(self._run_data_cid)
        assert isinstance(res, core.WorkflowResult)
        return typing.cast(R, res.result)


def _find_env_file(config_path: Path) -> Path | None:
    """Find .env file by walking up from config directory."""
    current_dir = config_path.parent
    while current_dir != current_dir.parent:  # Stop at root
        env_file = current_dir / ".env"
        if env_file.exists():
            return env_file
        current_dir = current_dir.parent
    return None


class SecretsProvider(provider_base.ImplProvider):
    """Provider for secrets using SecretFromEnvVarDeclaration system.

    This provider reads secrets from environment variables based on
    SecretFromEnvVarDeclaration or SecretFromOptionalEnvVarDeclaration entities.

    Args:
        config: The fxtr configuration (used for .env file loading)
        declarations: Dictionary mapping each secret handle to its declaration.
                     Should have exactly one declaration per handle (no conflicts).
    """

    def __init__(
        self,
        config: fxtrconfig.FxtrConfig,
        declarations: dict[
            core.ExternalServiceIdentifier[core.SecretRetriever[Any]],
            core.SecretFromEnvVarDeclaration | core.SecretFromOptionalEnvVarDeclaration,
        ],
    ):
        self._config = config
        self._declarations = dict(declarations)
        self._env_vars = self._load_env_vars()

    def _load_env_vars(self) -> dict[str, str]:
        """Load environment variables from .env file if configured."""
        env_vars = dict(os.environ)
        if self._config.resolver.secrets.use_env_file:
            env_file_path = _find_env_file(self._config.config_path)
            if env_file_path and env_file_path.exists():
                vals = dotenv.dotenv_values(env_file_path)
                env_vars.update({k: v for k, v in vals.items() if v is not None})
        return env_vars

    def update_declarations(
        self,
        declarations: dict[
            core.ExternalServiceIdentifier[core.SecretRetriever[Any]],
            core.SecretFromEnvVarDeclaration | core.SecretFromOptionalEnvVarDeclaration,
        ],
    ) -> None:
        """Update the declarations used by this provider.

        This allows the provider to be reused across multiple launches with different
        declarations without recreating the provider instance.
        """
        self._declarations = dict(declarations)
        # Reload env vars in case .env file has changed
        self._env_vars = self._load_env_vars()

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        """Provide implementation for a secret plan."""
        # Validate plan type
        if not isinstance(plan, core.ExternalServiceIdentifier):
            raise provider_base.ProviderUnsupportedPlanError(
                f"SecretsProvider only supports ExternalServiceIdentifier plans, got {type(plan)}"
            )

        # Check that the plan is in our declarations
        if plan not in self._declarations:
            raise provider_base.ProviderUnsupportedPlanError(
                "SecretsProvider does not have a declaration for plan"
                f" {plan.service_id} (namespace: {plan.namespace_id})"
            )

        declaration = self._declarations[plan]
        env_var = declaration.env_var
        is_optional = isinstance(declaration, core.SecretFromOptionalEnvVarDeclaration)

        # Try to read the environment variable
        if env_var not in self._env_vars:
            if is_optional:
                # Optional secret - return None
                secret_value = None
            else:
                # Required secret - error
                raise RuntimeError(
                    f"Required environment variable '{env_var}' not found for secret"
                    f" {plan.service_id} (namespace: {plan.namespace_id})"
                )
        else:
            secret_value = self._env_vars[env_var]

        # Create implementation
        impl = SecretRetrieverImpl(secret_value, is_optional)

        # Create provenance
        declaration_cid = core.entity_cid(declaration)
        provenance = SecretDeclarationProvenance(
            declaration_cid=declaration_cid,
            env_var=env_var,
        )

        # Validate provenance_lock if provided
        if provenance_lock is not None:
            if not isinstance(provenance_lock, SecretDeclarationProvenance):
                raise RuntimeError(
                    "SecretsProvider received provenance_lock of type "
                    f"{type(provenance_lock).__name__}, expected SecretDeclarationProvenance or"
                    " None"
                )
            if provenance_lock.declaration_cid != declaration_cid:
                raise RuntimeError(
                    "SecretsProvider provenance_lock mismatch: "
                    f"expected declaration {declaration_cid.encode('base32')}, "
                    f"got {provenance_lock.declaration_cid.encode('base32')}"
                )

        return impl, provenance


class TaskResultHandleProvider(provider_base.ImplProvider):
    """Provider for task result handles from run data.

    This provider looks up existing runs for tasks from the active runset and provides
    access to their data. It does NOT execute new runs - it only retrieves data from
    completed runs in the runset.

    Args:
        config: The fxtr configuration
    """

    def __init__(
        self,
        config: fxtrconfig.FxtrConfig,
    ):
        self._config = config

        # Set up locator and loader
        self._run_data_locator = locator.ClosedBinaryCBOROnDiskLocator(
            fxtrconfig.get_active_entity_store(config)
        )
        self._run_data_loader = locator.CachingEntityLoader.from_locator(self._run_data_locator)

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        """Provide implementation for a task result handle.

        If provenance_lock is a RunProvenance, use that run's data directly.
        If provenance_lock is None, look up the run in the active runset.
        Otherwise, raise an error.
        """
        if not isinstance(plan, core.TaskResultHandle):
            raise provider_base.ProviderUnsupportedPlanError(
                f"TaskResultHandleProvider only supports TaskResultHandle plans, got {type(plan)}"
            )

        task_cid = plan.task_ref

        # Determine which run to use based on provenance_lock
        if provenance_lock is not None:
            # If provenance_lock is provided, it must be a RunProvenance
            if not isinstance(provenance_lock, RunProvenance):
                raise RuntimeError(
                    "TaskResultHandleProvider received provenance_lock of type "
                    f"{type(provenance_lock).__name__}, expected RunProvenance or None"
                )
            # Use the run specified in the provenance lock
            run_cid = provenance_lock.run_cid
            provenance = provenance_lock
        else:
            # Load the active runset and look up the run
            runset = api_lib.load_active_runset(self._config)

            # Look up the run for this task in the runset
            if task_cid not in runset.runs:
                raise RuntimeError(
                    f"Task {task_cid.encode('base32')} is not in the active runset. "
                    "Use `fxtr runset adopt` to add it, or run the task with `fxtr materialize`."
                )

            run_cid = runset.runs[task_cid]
            provenance = RunProvenance(run_cid=run_cid)

        # Load the run to get the run_data CID
        run = self._run_data_loader(run_cid)
        assert isinstance(run, core.Run)

        # Create implementation
        impl = RunDataRetriever(run.run_data, self._run_data_loader, plan._handle_spec)

        return impl, provenance


class ConstantProvenance(
    core.DataEntityBase, namespace_id="!builtin.provenance", type_id="ConstantProvenance^1"
):
    """Provenance for constant values."""


@dataclasses.dataclass(frozen=True)
class ConstantRetriever[R: core.EntityBase](core.StorageHandle[R]):
    """Implementation of a run data retriever."""

    _value: R

    def interface_spec(self) -> core.InterfaceSpec[core.StorageHandle[R]]:
        return core.spec_of(core.StorageHandle[type(self._value)])

    async def retrieve(self) -> R:
        return self._value


class ConstantProvider(provider_base.ImplProvider):
    """Provider for constant values."""

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        """Provide implementation for a constant value."""
        if not isinstance(plan, core.ConstantHandle):
            raise provider_base.ProviderUnsupportedPlanError(
                f"ConstantProvider only supports ConstantHandle plans, got {type(plan)}"
            )

        # Create implementation
        impl = ConstantRetriever(plan.value)
        provenance = ConstantProvenance()

        if provenance_lock is not None and provenance_lock != provenance:
            raise RuntimeError(
                "ConstantProvider received provenance_lock of type "
                f"{type(provenance_lock).__name__}, expected ConstantProvenance or None"
            )

        return impl, provenance


class RuntimeEnvProvenance(
    core.DataEntityBase,
    namespace_id="!builtin.provenance",
    type_id="RuntimeEnvProvenance^1",
):
    """Provenance for runtime environment configuration.

    Attributes:
        assignment_cid: The CID of the RuntimeEnvConfigAssignment that was used.
    """

    assignment_cid: cbrrr.CID


@dataclasses.dataclass(frozen=True)
class RuntimeEnvRetrieverImpl[T: core.AnyFrozenIPLDable](core.RuntimeEnvRetriever[T]):
    """Implementation of RuntimeEnvRetriever that returns a configured value.

    The interface spec is stored during initialization from the plan being implemented.
    """

    _value: T
    _interface_spec: core.InterfaceSpec[core.RuntimeEnvRetriever[T]]

    def interface_spec(self) -> core.InterfaceSpec[core.RuntimeEnvRetriever[T]]:
        """Return the stored interface spec."""
        return self._interface_spec

    async def get_runtime_config(self) -> T:
        return self._value


class RuntimeEnvConfigProvider(provider_base.ImplProvider):
    """Provider for runtime environment configuration.

    This provider takes a dictionary mapping ExternalServiceIdentifier[RuntimeEnvRetriever[T]]
    to RuntimeEnvConfigAssignment[T] entities, and provides implementations that return
    the configured values.

    Args:
        assignments: Dictionary mapping each runtime env plan to its active assignment.
                    Should have exactly one assignment per plan (no conflicts).
    """

    def __init__(
        self,
        assignments: dict[
            core.ExternalServiceIdentifier[core.RuntimeEnvRetriever[Any]],
            core.RuntimeEnvConfigAssignment,
        ],
    ):
        self._assignments = dict(assignments)

    def update_assignments(
        self,
        assignments: dict[
            core.ExternalServiceIdentifier[core.RuntimeEnvRetriever[Any]],
            core.RuntimeEnvConfigAssignment,
        ],
    ) -> None:
        """Update the assignments used by this provider.

        This allows the provider to be reused across multiple launches with different
        assignments without recreating the provider instance.
        """
        self._assignments = dict(assignments)

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        """Provide implementation for a runtime env config plan."""
        if not isinstance(plan, core.ExternalServiceIdentifier):
            raise provider_base.ProviderUnsupportedPlanError(
                "RuntimeEnvConfigProvider only supports ExternalServiceIdentifier plans, got"
                f" {type(plan)}"
            )

        # Check that the plan is in our assignments
        if plan not in self._assignments:
            raise provider_base.ProviderUnsupportedPlanError(
                "RuntimeEnvConfigProvider does not have an assignment for plan"
                f" {plan.service_id} (namespace: {plan.namespace_id})"
            )

        assignment = self._assignments[plan]

        # Create implementation that returns the configured value
        # Pass the plan's interface spec to the implementation
        impl = RuntimeEnvRetrieverImpl(assignment.value, plan.interface_spec)

        # Create provenance
        assignment_cid = core.entity_cid(assignment)
        provenance = RuntimeEnvProvenance(assignment_cid=assignment_cid)

        # Validate provenance_lock if provided
        if provenance_lock is not None:
            if not isinstance(provenance_lock, RuntimeEnvProvenance):
                raise RuntimeError(
                    "RuntimeEnvConfigProvider received provenance_lock of type "
                    f"{type(provenance_lock).__name__}, expected RuntimeEnvProvenance or None"
                )
            if provenance_lock.assignment_cid != assignment_cid:
                raise RuntimeError(
                    "RuntimeEnvConfigProvider provenance_lock mismatch: "
                    f"expected assignment {assignment_cid.encode('base32')}, "
                    f"got {provenance_lock.assignment_cid.encode('base32')}"
                )

        return impl, provenance


class RuntimeCoordinatorProvenance(
    core.DataEntityBase,
    namespace_id="!builtin.provenance",
    type_id="RuntimeCoordinatorProvenance^1",
):
    """Provenance for a runtime coordinator object.

    Runtime coordinator bindings are resolved to plans dynamically when tasks are launched,
    separate from the binding of the task. The provenance for a coordinator describes both the
    plan that was bound, and the provenance of the implementations of that plan and its
    dependencies.

    Attributes:
        bound_plan: The plan that was bound to the coordinator.
        bound_provenances: The provenances of the implementation of the bound plan and its
            dependencies.
    """

    bound_plan: core.Plan[Any]
    bound_provenances: core.ImplProvenanceSet
