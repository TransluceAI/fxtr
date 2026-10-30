"""Backend runner for managing task execution and plan lifecycle."""

from __future__ import annotations

import abc
import contextlib
import dataclasses
import datetime
import functools
import logging
import pathlib
import typing
import weakref
from typing import (
    AbstractSet,
    Any,
    AsyncGenerator,
    Callable,
    Collection,
    Literal,
    Mapping,
    Sequence,
)

import anyio
import anyio.abc
import cbrrr
from frozendict import frozendict

from fxtr.object_model import dag_util

from ..builtin_util import watchable
from ..impl_plans import plan_dep_util, py_impl_plan
from ..object_model import collection_tracking, core, locator
from ..providers import externals_provider, provider_base, uv_py_provider
from . import api_lib, fxtrconfig

logger = logging.getLogger(__name__)


# Protocol definitions for remote access


class TaskWaiter(
    core.FxtrCallTarget,
    interface_id="TaskWaiter^1",
    namespace_id="!builtin.backend_runner",
    stateless=True,
):
    """Protocol for waiting on task completion."""

    @abc.abstractmethod
    async def wait(self) -> tuple[str, cbrrr.CID | None, str]:
        """Wait for the task to complete.

        Returns:
            Tuple of (status, run_cid, status_message) where:
            - status: Status string indicating task outcome (e.g., "completed", "failed", "cancelled")
            - run_cid: CID of the run if available, None otherwise
            - status_message: Human-readable status message
        """
        ...


class WorkflowStepLaunch(
    core.DataEntityBase, type_id="WorkflowStepLaunch^1", namespace_id="!builtin.backend_runner"
):
    """Wrapper indicating a request to launch a single step of a workflow.

    This object can be used as part of a call to BackendRunner.launch() to request the execution of
    a single step of a workflow.

    Attributes:
        workflow: The workflow (step) to launch.
    """

    workflow: core.Workflow


class FullWorkflowLaunch(
    core.DataEntityBase, type_id="FullWorkflowLaunch^1", namespace_id="!builtin.backend_runner"
):
    """Wrapper indicating a request to launch a full workflow.

    This object can be used as part of a call to BackendRunner.launch() to request the execution of
    a full workflow, which includes:
    - launching the first step of the workflow if needed
    - if the first step produced a WorkflowState:
        - launching any tasks produced by the first step,
        - launching the full workflow starting from the next step (which recursively applies the
          above logic)

    Conceptually, launching a FullWorkflowLaunch is the equivalent of launching a task, in that
    it produces the result that is available at the `result_handle` of the workflow. Similarly, if
    the `result_handle` of the workflow _already_ resolves to something in the runset, then it is
    not allowed to launch a FullWorkflowLaunch for that workflow.
    """

    workflow: core.Workflow


class BackendRunnerInterface(
    core.FxtrCallTarget,
    interface_id="BackendRunnerInterface^1",
    namespace_id="!builtin.backend_runner",
    stateless=False,
):
    """Protocol for remote access to BackendRunner."""

    @abc.abstractmethod
    async def activate_collection(self, collection: collection_tracking.Collection | str) -> None:
        """Activate a collection and extract runtime/secret bindings from it.

        This method discovers all external service declarations, assignments, and bindings
        from the provided collection and its dependencies, validates that there are no
        conflicts, and caches them for use in subsequent launch() or request() calls.

        Args:
            collection: Either a Collection object or a string collection name from config.
                       If a string, it will be loaded from self._config.blueprint.sources.

        Raises:
            RuntimeError: If collection name not found in config, or if multiple
                         declarations/assignments/bindings found for the same handle/plan.
        """
        ...

    @abc.abstractmethod
    async def launch(
        self,
        tasks: Sequence[core.Task | WorkflowStepLaunch | FullWorkflowLaunch],
        message: str | None = None,
    ) -> dict[str, TaskWaiter]:
        """Launch tasks for execution.

        Uses the runtime/secret bindings previously set via activate_collection().

        Args:
            tasks: List of tasks to launch. Can also include workflows (but in this case the
                caller must tag the requests as either requests for a single step of the workflow
                or requests for the full workflow.)
            message: Optional message for the run

        Returns:
            Dictionary mapping task CID strings (base32-encoded) to their TaskWaiter implementations
        """
        ...

    @abc.abstractmethod
    async def watch(self) -> watchable.Watchable:
        """Get a watchable for state updates.

        Returns:
            A Watchable that publishes state updates
        """
        ...

    @abc.abstractmethod
    async def request[T: core.FxtrCallTarget](self, plan: core.Plan[T]) -> T:
        """Request an implementation of a plan.

        Args:
            plan: The plan to request.

        Returns:
            The implementation of the plan.
        """
        ...


class TaskDataNotAvailableError(Exception):
    """Exception raised when task data is not available."""


# State type definitions


@dataclasses.dataclass
class PlanWorkerState:
    """State for a plan worker."""

    plan: core.Plan[Any]
    dep_states: dict[core.Plan[Any], PlanWorkerState]
    ready_event: anyio.Event
    shutdown_event: anyio.Event
    provider: provider_base.ImplProvider
    user_count: int
    status: Literal["pending", "setup", "ready", "failed", "cancelled"]
    message: str
    check_usable: Callable[[], bool]
    # Set when ready
    plan_provenance: core.AnyEntity | None = None
    impl: core.FxtrCallTarget | None = None


@dataclasses.dataclass
class TaskWorkerState:
    """State for a task worker.

    Task workers are responsible for running tasks, or for running a single step of a workflow.
    """

    task: core.Task | core.Workflow
    task_function_state: PlanWorkerState
    all_dep_states: dict[core.Plan[Any], PlanWorkerState]
    task_result_handle_deps: AbstractSet[
        core.TaskResultHandle | core.WorkflowResultHandle | core.WorkflowStepResultHandle
    ]
    done_event: anyio.Event
    status: Literal["pending", "waiting", "running", "completed", "failed", "cancelled"]
    status_message: str
    run_message: str | None
    # Set on successful completion
    run_cid: core.CID_Of[core.Run] | None = None


@dataclasses.dataclass
class FullWorkflowWorkerState:
    """State for a workflow worker.

    Workflow workers iteratively launch new workflow steps as they are completed, and also launch
    all tasks that are contained in each of the step data.

    Attributes:
        current_step: The current step of the workflow. Each FullWorkflowWorkerState manages a
            single step of the workflow, but delegates future work to new workers.
        current_step_state: The state of the current step's task.
        rest_of_workflow_state: The state of the next step of the workflow, once it has been started.
        done_event: Event that is set when the _full workflow_ has completed. All workers started
            from the same original FullWorkflowLaunch request will share a single done_event.
            If it is set, users can walk the chain of rest_of_workflow_state objects to get the final
            task and its result.
        status: The status of the workflow. If "in_step", consult the current step for the status.
            If "pending", the workflow worker has not been started yet.
        run_message: A message describing the run of the workflow.
    """

    current_step: core.Workflow
    current_step_state: TaskWorkerState
    rest_of_workflow_state: FullWorkflowWorkerState | None
    done_event: anyio.Event
    status: Literal["pending", "in_step", "cancelled", "done", "in_rest"]
    run_message: str | None


# TaskWaiter implementation


class TaskWaiterImpl(TaskWaiter):
    """Implementation of TaskWaiter that wraps a TaskWorkerState."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(self, state: TaskWorkerState):
        self._state = state

    async def wait(self) -> tuple[str, cbrrr.CID | None, str]:
        """Wait for the task to complete and return status, run_cid, and status_message."""
        await self._state.done_event.wait()
        return self._state.status, self._state.run_cid, self._state.status_message


class WorkflowWaiterImpl(TaskWaiter):
    """Implementation of TaskWaiter that wraps a FullWorkflowWorkerState."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(self, state: FullWorkflowWorkerState):
        self._state = state

    async def wait(self) -> tuple[str, cbrrr.CID | None, str]:
        """Wait for the task to complete and return status, run_cid, and status_message."""
        await self._state.done_event.wait()
        # note: the run CID of the final task is the run CID of the full workflow
        return (
            self._state.status,
            self._state.current_step_state.run_cid,
            self._state.current_step_state.status_message,
        )


# TaskResultHandle provider implementations


class RunsetTaskResultProvider(provider_base.ImplProvider):
    """Provider that retrieves task results from the runset."""

    def __init__(
        self,
        plan: core.TaskResultHandle | core.WorkflowStepResultHandle,
        run_cid: cbrrr.CID,
        run_data_loader: locator.CachingEntityLoader,
    ):
        self._plan = plan
        self._run_cid = run_cid
        self._run_data_loader = run_data_loader

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        assert plan == self._plan
        run = self._run_data_loader(self._run_cid)
        assert isinstance(run, core.Run)
        impl = externals_provider.RunDataRetriever(
            run.run_data, self._run_data_loader, self._plan._handle_spec
        )
        provenance = externals_provider.RunProvenance(run_cid=self._run_cid)
        return impl, provenance

    def check_usable_against(self, runset: core.RunSet | None):
        match self._plan:
            case core.TaskResultHandle():
                task_cid = self._plan.task_ref
            case core.WorkflowStepResultHandle():
                task_cid = self._plan.workflow_ref
            case _:
                raise ValueError(f"Unsupported plan type: {type(self._plan).__name__}")
        return runset is not None and runset.runs.get(task_cid) == self._run_cid


class ActiveTaskResultProvider(provider_base.ImplProvider):
    """Provider that waits for an active task to complete."""

    def __init__(self, task_state: TaskWorkerState, run_data_loader: locator.CachingEntityLoader):
        self._task_state = task_state
        self._run_data_loader = run_data_loader

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        assert isinstance(plan, core.TaskResultHandle | core.WorkflowStepResultHandle)
        await self._task_state.done_event.wait()
        if self._task_state.run_cid is None:
            raise TaskDataNotAvailableError("Task did not complete successfully")
        run_cid = self._task_state.run_cid
        run = self._run_data_loader(run_cid)
        assert isinstance(run, core.Run)
        impl = externals_provider.RunDataRetriever(
            run.run_data, self._run_data_loader, plan._handle_spec
        )
        provenance = externals_provider.RunProvenance(run_cid=run_cid)
        return impl, provenance


class RunsetWorkflowResultProvider(provider_base.ImplProvider):
    """Provider that retrieves workflow results from the runset."""

    def __init__(
        self,
        plan: core.WorkflowResultHandle,
        workflow_path: list[tuple[core.CID_Of[core.Workflow], core.CID_Of[core.Run]]],
        run_data_loader: locator.CachingEntityLoader,
    ):
        self._plan = plan
        self._workflow_path = workflow_path
        self._run_data_loader = run_data_loader

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        assert plan == self._plan
        run = self._run_data_loader(self._workflow_path[-1][1])
        assert isinstance(run, core.Run)
        impl = externals_provider.RunDataRetriever(
            run.run_data, self._run_data_loader, self._plan._handle_spec
        )
        provenance = externals_provider.WorkflowResultProvenance(
            step_run_cids=tuple(rcid for _, rcid in self._workflow_path)
        )
        return impl, provenance

    def check_usable_against(self, runset: core.RunSet | None):
        if runset is None:
            return False
        for workflow_ref, run_cid in self._workflow_path:
            if runset.runs.get(workflow_ref) != run_cid:
                return False
        return True


class ActiveWorkflowResultProvider(provider_base.ImplProvider):
    """Provider that waits for an active workflow to complete."""

    def __init__(
        self,
        plan: core.WorkflowResultHandle,
        existing_workflow_path: list[tuple[core.CID_Of[core.Workflow], core.CID_Of[core.Run]]],
        workflow_state: FullWorkflowWorkerState,
        run_data_loader: locator.CachingEntityLoader,
    ):
        self._plan = plan
        self._existing_workflow_path = existing_workflow_path
        self._workflow_state = workflow_state
        self._run_data_loader = run_data_loader

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        assert plan == self._plan
        await self._workflow_state.done_event.wait()
        # Collect steps.
        step_run_cids = [rcid for _, rcid in self._existing_workflow_path]
        step_state = self._workflow_state
        while step_state.rest_of_workflow_state is not None:
            assert step_state.current_step_state.run_cid is not None
            step_run_cids.append(step_state.current_step_state.run_cid)
            step_state = step_state.rest_of_workflow_state

        if step_state.current_step_state.run_cid is None:
            raise TaskDataNotAvailableError("Workflow did not complete successfully")
        run_cid = step_state.current_step_state.run_cid
        run = self._run_data_loader(run_cid)
        assert isinstance(run, core.Run)
        impl = externals_provider.WorkflowResultRetriever(
            run.run_data, self._run_data_loader, self._plan._handle_spec
        )
        provenance = externals_provider.WorkflowResultProvenance(step_run_cids=tuple(step_run_cids))
        return impl, provenance


def _summarize(value: core.AnyEntity) -> str:
    if isinstance(value, py_impl_plan.PyImplPlan):
        summary = (
            f"PyImplPlan(constructor={repr(value.constructor)}, kwargs={repr(value.kwargs)}, ...)"
        )
    elif isinstance(value, core.ExternalServiceIdentifier):
        summary = f"ExternalServiceIdentifier(service_id={repr(value.service_id)}, ...)"
    elif isinstance(value, core.TaskResultHandle):
        summary = f"TaskResultHandle(task_ref={repr(value.task_ref)}, ...)"
    elif isinstance(value, core.WorkflowResultHandle):
        summary = f"WorkflowResultHandle(workflow_ref={repr(value.workflow_ref)}, ...)"
    elif isinstance(value, core.WorkflowStepResultHandle):
        summary = f"WorkflowStepResultHandle(workflow_ref={repr(value.workflow_ref)}, ...)"
    elif isinstance(value, core.Run):
        summary = f"Run(run_data={repr(value.run_data)}, ...)"
    else:
        summary = f"{type(value).__name__}(...)"
    if len(summary) > 100:
        summary = summary[:100] + "..."
    return summary + f"[cid={core.entity_cid(value).encode('base32')}]"


# Forwarder class


class _BackendRunnerPlanForwarder(core.FxtrCallTarget):
    """Forwarder for a plan implementation from the BackendRunner."""

    def __init__(self, impl: core.FxtrCallTarget):
        self._impl = impl

    def interface_spec(self) -> core.InterfaceSpec[core.FxtrCallTarget]:
        return self._impl.interface_spec()

    def __getattr__(self, method: str) -> Any:
        """Proxy method calls to the implementation."""
        return getattr(self._impl, method)


# Main BackendRunner class


def _toposort_tasks_and_workflows(
    tasks_and_workflows: Collection[core.Task | core.Workflow],
) -> list[core.Task | core.Workflow]:
    """Topologically sort a list of tasks and workflows by dependency order."""
    cid_to_task: dict[cbrrr.CID, core.Task | core.Workflow] = {}
    for task_or_workflow in tasks_and_workflows:
        assert isinstance(task_or_workflow, core.Task | core.Workflow)
        cid_to_task[core.entity_cid(task_or_workflow)] = task_or_workflow

    task_to_remaining_deps: dict[core.Task | core.Workflow, set[core.Task | core.Workflow]] = {}
    dep_to_requestors: dict[core.Task | core.Workflow, set[core.Task | core.Workflow]] = {}
    ready = []
    for task_or_workflow in tasks_and_workflows:
        task_to_remaining_deps[task_or_workflow] = set()
        plan = task_or_workflow.function
        deps = plan_dep_util.plan_deps(plan, recursive=True)
        for dep in deps:
            match dep:
                case (
                    core.TaskResultHandle(task_ref=ref)
                    | core.WorkflowStepResultHandle(workflow_ref=ref)
                    | core.WorkflowResultHandle(workflow_ref=ref)
                ):
                    if ref in cid_to_task:
                        task_to_remaining_deps[task_or_workflow].add(cid_to_task[ref])
                        dep_to_requestors[cid_to_task[ref]].add(task_or_workflow)
        if len(task_to_remaining_deps[task_or_workflow]) == 0:
            ready.append(task_or_workflow)

    result = []
    while ready:
        task = ready.pop()
        result.append(task)
        for later in dep_to_requestors[task]:
            task_to_remaining_deps[later].remove(task)
            if len(task_to_remaining_deps[later]) == 0:
                ready.append(later)
        del dep_to_requestors[task], task_to_remaining_deps[task]

    assert not task_to_remaining_deps, "Didn't process all tasks and workflows?"
    return result


def _extract_tasks_from_workflow_state(
    workflow_state: core.WorkflowState[Any, Any],
) -> tuple[list[core.Task | core.Workflow], core.Workflow]:
    """Extract the next tasks from a workflow step execution result."""
    # Extract tasks and workflows
    collector = dag_util.RecursiveSubgraphCollector(
        include_if=lambda ent: not isinstance(ent, (core.Task, core.Workflow))
    )
    collector.visit_entity(workflow_state.step_data)
    tasks_and_workflows = set[core.Task | core.Workflow](
        [
            *typing.cast(list[core.Task | core.Workflow], collector.excluded_entities),
            workflow_state.next,
        ]
    )
    # Sort them by dependency order
    sorted_tasks_and_workflows = _toposort_tasks_and_workflows(tasks_and_workflows)
    return sorted_tasks_and_workflows, workflow_state.next


class BackendRunner(BackendRunnerInterface):
    """Backend runner for managing task execution and plan lifecycle.

    The BackendRunner is responsible for:
    - Managing the lifecycle of plan workers and task workers
    - Tracking user counts for plans and shutting them down when no longer needed
    - Publishing state updates via a Watchable
    - Coordinating with the active runset

    Backend runners maintain both active and stale plan and task workers. These have similar but
    somewhat different meanings.
    - Active plan workers/states refer to workers that are currently running and providing
      implementations of particular plans, where those implementations are the current best version
      of the plan using the current runtime state.
    - Stale plan workers/states refer to workers that are running, but are not providing the
      most up-to-date version of the implementation for the plan. Stale plans may depend on other
      stale plans or on active plans. We keep them running until all dependencies finish, but no
      longer allow new dependencies.
    - Active task workers/states refer to tasks that are currently running or waiting to run,
      where the results of those tasks will be the ones added to the current runset (as far as we
      know).
    - Stale task worker/states refer to tasks that are currently running or waiting to run, but
      where those results are not consistent with the current runset state.
    """

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        config: fxtrconfig.FxtrConfig,
        task_group: anyio.abc.TaskGroup,
        shutdown_event: anyio.Event,
        update_publisher: Callable[[core.SendableValue], None],
        update_subscriber: watchable.Watchable,
    ):
        self._config = config
        self._task_group = task_group
        self._shutdown_event = shutdown_event
        self._update_publisher = update_publisher
        self._update_subscriber = update_subscriber

        # State tracking
        self._active_plans: dict[core.Plan[Any], PlanWorkerState] = {}
        self._stale_plan_states: dict[int, PlanWorkerState] = {}
        self._active_tasks: dict[core.Task | core.Workflow, TaskWorkerState] = {}
        self._stale_task_states: dict[int, TaskWorkerState] = {}
        self._active_workflows: dict[core.Workflow, FullWorkflowWorkerState] = {}
        self._stale_workflow_states: dict[int, FullWorkflowWorkerState] = {}
        self._pending_sub_shutdown_events: set[anyio.Event] = set()

        # External service assignments cache
        self._active_secret_declarations: dict[
            core.ExternalServiceIdentifier[core.SecretRetriever[Any]],
            core.SecretFromEnvVarDeclaration | core.SecretFromOptionalEnvVarDeclaration,
        ] = {}
        self._active_runtime_env_assignments: dict[
            core.ExternalServiceIdentifier[core.RuntimeEnvRetriever[Any]],
            core.RuntimeEnvConfigAssignment,
        ] = {}
        self._active_runtime_coordinator_bindings: dict[
            core.ExternalServiceIdentifier[Any],
            core.RuntimeCoordinatorBinding,
        ] = {}

        # Synchronization
        self._state_lock = anyio.Lock()

        # Cache for plan usability checks (keyed by id(state))
        self._plan_usability_cache: dict[int, bool] = {}

        # Cache for task/workflow runset compatibility checks (keyed by id(state))
        self._task_runset_compatibility_cache: dict[int, bool] = {}

        # Cached runset at launch time (used for verification, not for actual task execution)
        self._runset_at_launch: core.RunSet | None = None

        # Provider infrastructure (similar to task_launcher.py)
        self._run_data_locator = api_lib.get_active_data_locator(config)
        self._run_data_loader = locator.CachingEntityLoader.from_locator(self._run_data_locator)

        # Will be set up in setup context manager
        self._uv_provider: uv_py_provider.UVManagedPyImplProvider | None = None
        self._secrets_provider: externals_provider.SecretsProvider | None = None
        self._runtime_env_provider: externals_provider.RuntimeEnvConfigProvider | None = None

        self._task_group.start_soon(self._cleanup_pending_sub_shutdown_events)

    async def _cleanup_pending_sub_shutdown_events(self) -> None:
        """Cleanup pending sub shutdown events."""
        await self._shutdown_event.wait()
        for event in self._pending_sub_shutdown_events:
            event.set()

    @classmethod
    @contextlib.asynccontextmanager
    async def create(
        cls,
        config: fxtrconfig.FxtrConfig,
    ) -> AsyncGenerator[BackendRunner, None]:
        """Create a BackendRunner with proper lifecycle management."""
        shutdown_event = anyio.Event()

        async with contextlib.AsyncExitStack() as stack:
            task_group = await stack.enter_async_context(anyio.create_task_group())
            publisher, subscriber = stack.enter_context(watchable.publishing_watchable())
            runner = cls(config, task_group, shutdown_event, publisher, subscriber)
            await runner._setup_providers(stack)
            try:
                yield runner
            finally:
                shutdown_event.set()

    async def _setup_providers(self, stack: contextlib.AsyncExitStack) -> None:
        """Set up providers for plans."""
        # Set up UV provider for Python plans
        virtual_env_dir = pathlib.Path(self._config.provider.uv.virtual_env_dir)

        # Get UV configuration from fxtr config
        uv_config_dict: dict[str, object] = dict(self._config.provider.uv.custom_uv_config)

        # Get existing sources from custom config or create new dict
        existing_sources = uv_config_dict.get("sources", {})
        if isinstance(existing_sources, dict):
            sources: dict[str, object] = dict(existing_sources)
        else:
            sources = {}

        # Add local source packages from config
        config_dir = self._config.config_path.parent
        for pkg_name, rel_path in self._config.provider.uv.local_source_packages.items():
            sources[pkg_name] = {
                "path": str((config_dir / rel_path).resolve()),
                "editable": True,
            }

        # Add dynamic source packages from config
        for pkg_name in self._config.provider.uv.dynamic_source_packages:
            sources[pkg_name] = self._extract_uv_source_from_package(pkg_name)

        # Update sources in config (nondestructively)
        if sources:
            uv_config_dict["sources"] = sources

        # Create UV provider
        self._uv_provider = await stack.enter_async_context(
            uv_py_provider.running_uv_managed_provider(
                virtual_env_dir=virtual_env_dir,
                plan_provenance_pairs=None,  # Will prepare plans later
                uv_config=uv_config_dict if uv_config_dict else None,
                remove_env_on_exit=True,
            )
        )

        # Set up external service providers with empty initial state
        # These will be updated with actual declarations/assignments during launch()
        self._secrets_provider = externals_provider.SecretsProvider(
            config=self._config,
            declarations={},  # Will be updated during launch()
        )
        self._runtime_env_provider = externals_provider.RuntimeEnvConfigProvider(
            assignments={},  # Will be updated during launch()
        )

    @staticmethod
    def _extract_uv_source_from_package(package_name: str) -> dict[str, str | bool]:
        """Extract UV source configuration from a package using metadata.

        This is copied from task_launcher.py for now.
        """
        import json
        from importlib import metadata
        from pathlib import Path

        try:
            dist = metadata.distribution(package_name)
        except metadata.PackageNotFoundError:
            raise RuntimeError(f"Could not find package '{package_name}' in current environment")

        # Try PEP 610 metadata (present for direct URL / editable / VCS installs)
        direct_url = None
        # Find and read direct_url.json, if present
        for file in dist.files or ():
            if file.name == "direct_url.json":
                direct_url = json.loads((dist.locate_file(file)).read_text())
                break

        if not direct_url:
            raise RuntimeError(
                f"Package '{package_name}' does not have direct_url.json metadata. "
                "This package may not be installed in editable mode or from a direct URL."
            )

        url = direct_url.get("url", "")
        if url.startswith("file://") and "dir_info" in direct_url:
            # Extract local path from file:// URL
            local_path = url.removeprefix("file://")
            if not Path(local_path).exists():
                raise RuntimeError(
                    f"Package '{package_name}' direct_url.json points to non-existent path:"
                    f" {local_path}"
                )
            return {"path": local_path, "editable": direct_url["dir_info"]["editable"]}
        elif url.startswith("ssh://") and "vcs_info" in direct_url:
            vcs_info = direct_url["vcs_info"]
            if vcs_info.get("vcs") != "git" or "commit_id" not in vcs_info:
                raise NotImplementedError(
                    f"Package '{package_name}' has unrecognized VCS info in direct_url.json:"
                    f" {vcs_info}. For ssh:// URLs, only git URLs pointing to a known commit hash"
                    " are currently supported for dynamic source packages."
                )
            commit_id = vcs_info["commit_id"]
            assert isinstance(commit_id, str)
            return {"git": url, "rev": commit_id}
        else:
            raise NotImplementedError(
                f"Package '{package_name}' has unrecognized URL in direct_url.json: {url}. Only"
                " local file:// and git ssh:// URLs are currently supported for dynamic source"
                " packages."
            )

    # Public API methods

    async def activate_collection(self, collection: collection_tracking.Collection | str) -> None:
        """Activate a collection and extract runtime/secret bindings from it.

        This method discovers all external service declarations, assignments, and bindings
        from the provided collection and its dependencies, validates that there are no
        conflicts, and caches them for use in subsequent launch() calls.

        Args:
            collection: Either a Collection object or a string collection name from config.
                       If a string, it will be loaded from self._config.blueprint.sources.

        Raises:
            RuntimeError: If collection name not found in config, or if multiple
                         declarations/assignments/bindings found for the same handle/plan.
        """
        # Load collection if string
        if isinstance(collection, str):
            collection_name = collection
            if collection_name not in self._config.blueprint.sources:
                available = list(self._config.blueprint.sources.keys())
                raise RuntimeError(
                    f"Collection '{collection_name}' not found in config. "
                    f"Available collections: {available}"
                )
            source_config = self._config.blueprint.sources[collection_name]
            entrypoint = source_config.module
            coll = api_lib.load_collection_from_entrypoint(entrypoint)
        else:
            coll = collection

        # Discover external service declarations and assignments
        secret_declarations_by_handle = api_lib.discover_secret_env_var_declarations(coll)
        runtime_env_assignments_by_plan = api_lib.discover_runtime_env_config_assignments(coll)
        runtime_coordinator_bindings_by_handle = api_lib.discover_runtime_coordinator_bindings(coll)

        # Validate no conflicts - each handle/plan should have exactly one declaration/assignment/binding
        for handle, declarations in secret_declarations_by_handle.items():
            if len(declarations) > 1:
                raise RuntimeError(
                    f"Multiple secret declarations found for handle {handle.service_id} "
                    f"(namespace: {handle.namespace_id}). Found {len(declarations)} declarations."
                )

        for plan, assignments in runtime_env_assignments_by_plan.items():
            if len(assignments) > 1:
                raise RuntimeError(
                    f"Multiple runtime env assignments found for plan {plan.service_id} "
                    f"(namespace: {plan.namespace_id}). Found {len(assignments)} assignments."
                )

        for handle, bindings in runtime_coordinator_bindings_by_handle.items():
            if len(bindings) > 1:
                raise RuntimeError(
                    f"Multiple runtime coordinator bindings found for handle {handle.service_id} "
                    f"(namespace: {handle.namespace_id}). Found {len(bindings)} bindings."
                )

        # Convert to single-item dicts (now that we've validated no conflicts)
        secret_declarations = {
            handle: declarations[0]
            for handle, declarations in secret_declarations_by_handle.items()
            if declarations
        }
        runtime_env_assignments = {
            plan: assignments[0]
            for plan, assignments in runtime_env_assignments_by_plan.items()
            if assignments
        }
        runtime_coordinator_bindings = {
            handle: bindings[0]
            for handle, bindings in runtime_coordinator_bindings_by_handle.items()
            if bindings
        }

        # Update cache to exactly match new assignments
        async with self._state_lock:
            self._active_secret_declarations = secret_declarations
            self._active_runtime_env_assignments = runtime_env_assignments
            self._active_runtime_coordinator_bindings = runtime_coordinator_bindings

            # Update providers with new declarations/assignments
            if self._secrets_provider:
                self._secrets_provider.update_declarations(secret_declarations)
            if self._runtime_env_provider:
                self._runtime_env_provider.update_assignments(runtime_env_assignments)

    async def launch(
        self,
        tasks: Sequence[core.Task | WorkflowStepLaunch | FullWorkflowLaunch],
        message: str | None = None,
    ) -> dict[str, TaskWaiter]:
        """Launch tasks for execution.

        Uses the runtime/secret bindings previously set via activate_collection().

        Args:
            tasks: List of tasks to launch
            message: Optional message for the run

        Returns:
            Dictionary mapping task CID strings (base32-encoded) to their TaskWaiter implementations

        Raises:
            RuntimeError: If any task is already running
        """
        async with self._state_lock:
            # Clear caches and update runset at the start of each launch
            self._plan_usability_cache.clear()
            self._task_runset_compatibility_cache.clear()
            self._runset_at_launch = api_lib.load_active_runset(self._config)

            # Check for already-running tasks
            for task in tasks:
                if task in self._active_tasks or task in self._active_workflows:
                    raise RuntimeError(
                        f"Task {core.entity_cid(task).encode('base32')} is already running"
                    )
                if core.entity_cid(task) in self._runset_at_launch.runs:
                    raise RuntimeError(
                        f"Task {core.entity_cid(task).encode('base32')} is already in the runset"
                    )

            # Note: Runtime/secret bindings are set via activate_collection() before launch.
            # We don't proactively invalidate plans here. Instead, _request_plan
            # will lazily verify that existing plans are still usable when they are requested.

            # Collect all plans
            all_plans = set()
            for req in tasks:
                match req:
                    case core.Task():
                        all_plans.add(req.function)
                        all_plans.update(plan_dep_util.plan_deps(req.function, recursive=True))
                    case WorkflowStepLaunch(wf) | FullWorkflowLaunch(wf):
                        all_plans.add(wf.function)
                        all_plans.update(plan_dep_util.plan_deps(wf.function, recursive=True))
                    case _:
                        raise ValueError(f"Unsupported task type: {type(req).__name__}")

            # Check that all plans are supported
            for plan in all_plans:
                self._check_supports_plan(plan)

            # Prepare UV provider for Python plans
            py_plans = [p for p in all_plans if isinstance(p, py_impl_plan.PyImplPlan)]
            if py_plans and self._uv_provider:
                await self._uv_provider.prepare([(p, None) for p in py_plans])

            # Request task functions and create task workers
            task_waiters: dict[str, TaskWaiter] = {}
            for req in tasks:
                match req:
                    case core.Task():
                        task = req
                    case WorkflowStepLaunch(wf) | FullWorkflowLaunch(wf):
                        task = wf
                    case _:
                        raise ValueError(f"Unsupported task type: {type(req).__name__}")

                task_function_state = await self._request_plan(task.function)

                # Compute all dependencies and request them
                all_deps = plan_dep_util.plan_deps(task.function, recursive=True)
                all_dep_states = {}
                for dep in all_deps:
                    assert dep in self._active_plans
                    all_dep_states[dep] = self._active_plans[dep]

                # Extract result handles
                task_result_handle_deps = {
                    dep
                    for dep in all_deps
                    if isinstance(
                        dep,
                        core.TaskResultHandle
                        | core.WorkflowStepResultHandle
                        | core.WorkflowResultHandle,
                    )
                }

                task_state = TaskWorkerState(
                    task=task,
                    task_function_state=task_function_state,
                    all_dep_states=all_dep_states,
                    task_result_handle_deps=task_result_handle_deps,
                    done_event=anyio.Event(),
                    status="pending",
                    status_message="Task scheduled",
                    run_message=message,
                )
                self._active_tasks[task] = task_state
                self._task_group.start_soon(self._task_worker, task_state)

                match req:
                    case core.Task():
                        # Create TaskWaiter for this task with base32-encoded CID key
                        task_cid = core.entity_cid(task)
                        task_waiters[task_cid.encode("base32")] = TaskWaiterImpl(task_state)
                    case WorkflowStepLaunch(wf):
                        # Create TaskWaiter for the workflow (not the request)
                        wf_cid = core.entity_cid(wf)
                        task_waiters[wf_cid.encode("base32")] = TaskWaiterImpl(task_state)
                    case FullWorkflowLaunch(wf):
                        # Spawn a workflow worker to manage the rest of the workflow
                        workflow_worker_state = FullWorkflowWorkerState(
                            current_step=wf,
                            current_step_state=task_state,
                            rest_of_workflow_state=None,
                            done_event=anyio.Event(),
                            status="pending",
                            run_message=message,
                        )
                        self._active_workflows[wf] = workflow_worker_state
                        self._task_group.start_soon(self._workflow_worker, workflow_worker_state)
                        # Create a WorkflowWaiterImpl for the workflow request
                        wf_cid = core.entity_cid(wf)
                        task_waiters[wf_cid.encode("base32")] = WorkflowWaiterImpl(
                            workflow_worker_state
                        )
                    case _:
                        raise ValueError(f"Unsupported task type: {type(req).__name__}")

            self._publish_update()
            return task_waiters

    async def watch(self) -> watchable.Watchable:
        """Get a watchable for state updates."""
        return self._update_subscriber

    async def request[T: core.FxtrCallTarget](self, plan: core.Plan[T]) -> T:
        """Request an implementation of a plan."""
        async with self._state_lock:
            # Clear caches and update runset at the start of each launch
            self._plan_usability_cache.clear()
            self._task_runset_compatibility_cache.clear()
            self._runset_at_launch = api_lib.load_active_runset(self._config)
            worker_state = await self._request_plan(plan)
            try:
                await worker_state.ready_event.wait()
                if worker_state.status in ("failed", "cancelled"):
                    raise RuntimeError(
                        f"Plan {_summarize(plan)} failed to setup: {worker_state.message}"
                    )
                assert worker_state.impl is not None
                logger.debug(f"Constructing a request forwarder for {_summarize(plan)}")
                forwarder = _BackendRunnerPlanForwarder(worker_state.impl)
                # Increment user count and then schedule a cleanup to decrement it again when the
                # forwarder object dies.
                worker_state.user_count += 1
                weakref.finalize(
                    forwarder, self._schedule_cleanup_request_worker_state, worker_state
                )
                return typing.cast(T, forwarder)
            finally:
                self._decrement_plan_user_count(worker_state)

    def _schedule_cleanup_request_worker_state(self, worker_state: PlanWorkerState) -> None:
        """Schedule a cleanup of a request worker state."""
        if worker_state.shutdown_event.is_set():
            # Already shut down, don't need to clean up
            logger.debug(
                f"Already shut down, not scheduling cleanup of request worker state for"
                f" {_summarize(worker_state.plan)}"
            )
            return

        logger.debug(
            f"Scheduling cleanup of request worker state for {_summarize(worker_state.plan)}"
        )

        async def cleanup():
            async with self._state_lock:
                self._decrement_plan_user_count(worker_state)

        self._task_group.start_soon(cleanup)

    # Internal plan management methods
    def _verify_worker_is_usable(
        self,
        state: PlanWorkerState,
    ) -> bool:
        """Check if a plan worker is usable.

        Must be called with _state_lock held.

        A worker is usable if:
        - It is still in active_plans
        - Its check_usable() returns True
        - All its dependencies are usable (recursive check)

        If the worker is not usable and is in active_plans, it will be moved to stale_plan_states.

        Args:
            state: The plan worker state to check

        Returns:
            True if the worker is usable, False otherwise
        """
        # Check cache first
        if id(state) in self._plan_usability_cache:
            return self._plan_usability_cache[id(state)]

        # Check if still in active plans
        if state.plan not in self._active_plans or self._active_plans[state.plan] is not state:
            result = False
        # Check if this plan's check_usable returns True
        elif not state.check_usable():
            result = False
        else:
            # Recursively check all dependencies
            result = True
            for dep_state in state.dep_states.values():
                if not self._verify_worker_is_usable(dep_state):
                    result = False
                    break

        # If not usable and still in active plans, move to stale
        if not result and state.plan in self._active_plans:
            state.shutdown_event.set()
            self._stale_plan_states[id(state)] = state
            del self._active_plans[state.plan]

        # Store in cache
        self._plan_usability_cache[id(state)] = result

        return result

    def _verify_task_worker_is_runset_compatible(
        self,
        state: TaskWorkerState,
    ) -> bool:
        """Check if a task worker is runset-compatible.

        Must be called with _state_lock held.

        A task is runset-compatible if:
        - The task's run is not already in the runset (no conflicting run),
          UNLESS it's the run produced by this task itself
        - All TaskResultHandle plan workers are usable (which transitively
          ensures their runs are in the runset)

        If not compatible and in active_tasks, moves to stale_task_states.

        Args:
            state: The task worker state to check

        Returns:
            True if compatible, False otherwise
        """
        # Check cache first
        if id(state) in self._task_runset_compatibility_cache:
            return self._task_runset_compatibility_cache[id(state)]

        # Use cached runset from launch
        assert self._runset_at_launch is not None
        runset = self._runset_at_launch

        task_cid = core.entity_cid(state.task)

        # Check if task has a conflicting run in runset
        if task_cid in runset.runs:
            # If this task already completed and produced this run, that's fine
            if state.run_cid is None or runset.runs[task_cid] != state.run_cid:
                # Conflicting run exists
                result = False
            else:
                # This is our own run, continue checking
                result = True
        else:
            result = True

        # If still potentially compatible, check all TaskResultHandle dependencies
        if result:
            for handle in state.task_result_handle_deps:
                dep_plan_state = state.all_dep_states[handle]
                if not self._verify_worker_is_usable(dep_plan_state):
                    result = False
                    break

        # If not compatible and still in active tasks, move to stale
        if not result and state.task in self._active_tasks:
            self._stale_task_states[id(state)] = state
            del self._active_tasks[state.task]

        # Store in cache
        self._task_runset_compatibility_cache[id(state)] = result

        return result

    def _verify_prefixed_workflow_worker_is_runset_compatible(
        self,
        workflow_path: list[tuple[core.CID_Of[core.Workflow], core.CID_Of[core.Run]]],
        state: FullWorkflowWorkerState,
    ) -> bool:
        """Check if a workflow worker is runset-compatible and also has a valid workflow path.

        Must be called with _state_lock held.

        Args:
            workflow_path: The workflow path to check
            state: The workflow worker state to check

        Returns:
            True if compatible, False otherwise
        """
        # Use cached runset from launch
        assert self._runset_at_launch is not None
        runset = self._runset_at_launch

        # Check if all pre-worker workflow steps are still in the runset
        for workflow_ref, run_cid in workflow_path:
            if runset.runs.get(workflow_ref) != run_cid:
                return False

        # Check if this workflow worker is runset-compatible
        return self._verify_workflow_worker_is_runset_compatible(state)

    def _verify_workflow_worker_is_runset_compatible(
        self,
        state: FullWorkflowWorkerState,
    ) -> bool:
        """Check if a workflow worker is runset-compatible.

        Must be called with _state_lock held.

        A workflow is runset-compatible if, along the full chain of workflow steps, all of the task
        workers are runset-compatible.

        If not compatible and in active_workflows, moves to stale_workflow_states.

        Args:
            state: The workflow worker state to check

        Returns:
            True if compatible, False otherwise
        """
        # Check cache first
        if id(state) in self._task_runset_compatibility_cache:
            return self._task_runset_compatibility_cache[id(state)]

        # Use cached runset from launch
        assert self._runset_at_launch is not None
        runset = self._runset_at_launch

        ok = True

        # Check if this task's worker is runset-compatible
        if ok and not self._verify_task_worker_is_runset_compatible(state.current_step_state):
            ok = False

        # Check if any subsequent workflow steps are runset compatible
        if ok and state.rest_of_workflow_state is not None:
            if not self._verify_workflow_worker_is_runset_compatible(state.rest_of_workflow_state):
                ok = False

        # If not compatible and still in active tasks, move to stale
        if not ok and state in self._active_workflows:
            self._stale_workflow_states[id(state)] = state
            del self._active_workflows[state.current_step]

        # Store in cache
        self._task_runset_compatibility_cache[id(state)] = ok

        return ok

    async def _request_plan(self, plan: core.Plan[Any]) -> PlanWorkerState:
        """Request a plan, setting it up if not already active.

        Must be called with _state_lock held.
        """
        if plan in self._active_plans:
            # Already active, verify it's still usable
            state = self._active_plans[plan]
            if self._verify_worker_is_usable(state):
                # Still usable, increment user count
                state.user_count += 1
                return state
            else:
                # No longer usable, fall through to set up a new one
                pass

        # Need to set up (call _setup_plan while holding lock)
        return await self._setup_plan(plan)

    def _check_supports_plan(self, plan: core.Plan[Any]) -> None:
        """Check that a plan is supported by this backend runner.

        Raises:
            RuntimeError: If the plan type is not supported or if required
                         external service has no declaration/assignment
        """
        if isinstance(plan, py_impl_plan.PyImplPlan):
            # PyImplPlan is supported
            pass
        elif isinstance(plan, core.ExternalServiceIdentifier):
            # Check if it's a SecretRetriever or RuntimeEnvRetriever or RuntimeCoordinator
            if plan in self._active_secret_declarations:
                # SecretRetriever - supported and has declaration
                pass
            # Check if this is a runtime env config
            elif plan in self._active_runtime_env_assignments:
                # RuntimeEnvRetriever - supported and has assignment
                pass
            # Check if this is a runtime coordinator binding
            elif plan in self._active_runtime_coordinator_bindings:
                # RuntimeCoordinator - supported and has binding
                binding = self._active_runtime_coordinator_bindings[plan]

                # Recursively check that the bound plan and all its deps are supported
                self._check_supports_plan(binding.plan)
                for dep in plan_dep_util.plan_deps(binding.plan, recursive=True):
                    self._check_supports_plan(dep)
            else:
                # Not supported or missing declaration/assignment/binding
                raise RuntimeError(
                    f"ExternalServiceIdentifier plan {plan.service_id} "
                    f"(namespace: {plan.namespace_id}) does not have a "
                    "declaration, assignment, or binding. Ensure the collection provides "
                    "a SecretFromEnvVarDeclaration, RuntimeEnvConfigAssignment, or "
                    "RuntimeCoordinatorBinding for this plan."
                )
        elif isinstance(plan, core.TaskResultHandle | core.WorkflowStepResultHandle):
            # TaskResultHandle and WorkflowStepResultHandle are supported
            pass
        elif isinstance(plan, core.WorkflowResultHandle):
            # WorkflowResultHandle is supported
            pass
        elif isinstance(plan, core.ConstantHandle):
            # ConstantHandle is supported
            pass
        else:
            raise RuntimeError(f"Unsupported plan type: {type(plan).__name__}")

    async def _setup_plan(self, plan: core.Plan[Any]) -> PlanWorkerState:
        """Set up a plan worker.

        Must be called with _state_lock held.
        """
        # Recursively request all direct dependencies
        dep_states = {}
        for dep in plan_dep_util.plan_deps(plan, recursive=False):
            dep_states[dep] = await self._request_plan(dep)

        # For coordinator bindings, also add the bound plan as a dependency
        if (
            isinstance(plan, core.ExternalServiceIdentifier)
            and plan in self._active_runtime_coordinator_bindings
        ):
            binding = self._active_runtime_coordinator_bindings[plan]
            dep_states[binding.plan] = await self._request_plan(binding.plan)

        # Determine the provider and check_usable function for this plan
        if isinstance(plan, py_impl_plan.PyImplPlan):
            provider = self._uv_provider
            # PyImplPlan is always usable (doesn't depend on external config)
            check_usable = lambda: True
        elif isinstance(plan, core.ExternalServiceIdentifier):
            # Route based on whether it's a secret, runtime env, or runtime coordinator
            if plan in self._active_secret_declarations:
                provider = self._secrets_provider
                # Capture the current declaration for this plan
                expected_decl = self._active_secret_declarations[plan]
                # Check if the declaration still matches
                check_usable = lambda: self._active_secret_declarations.get(plan) == expected_decl
            elif plan in self._active_runtime_env_assignments:
                provider = self._runtime_env_provider
                # Capture the current assignment for this plan
                expected_assign = self._active_runtime_env_assignments[plan]
                # Check if the assignment still matches
                check_usable = (
                    lambda: self._active_runtime_env_assignments.get(plan) == expected_assign
                )
            elif plan in self._active_runtime_coordinator_bindings:
                # Create provider using SimpleCallableProvider
                provider = provider_base.SimpleCallableProvider(self._provide_coordinator_binding)
                # Capture the current binding for check_usable
                expected_binding = self._active_runtime_coordinator_bindings[plan]
                check_usable = (
                    lambda: self._active_runtime_coordinator_bindings.get(plan) == expected_binding
                )
            else:
                # Should have been caught by _check_supports_plan
                raise RuntimeError(f"Unsupported ExternalServiceIdentifier: {_summarize(plan)}")
        elif isinstance(plan, core.TaskResultHandle | core.WorkflowStepResultHandle):
            # Create TaskResultHandle provider and check_usable
            provider, check_usable = self._create_task_result_handle_provider(plan)
        elif isinstance(plan, core.WorkflowResultHandle):
            # Create WorkflowResultHandle provider and check_usable
            provider, check_usable = self._create_workflow_result_handle_provider(plan)
        elif isinstance(plan, core.ConstantHandle):
            provider = externals_provider.ConstantProvider()
            # ConstantHandle is always usable (immutable constant)
            check_usable = lambda: True
        else:
            raise RuntimeError(f"Unsupported plan type: {type(plan).__name__}")

        assert provider is not None

        # Create PlanWorkerState
        state = PlanWorkerState(
            plan=plan,
            dep_states=dep_states,
            ready_event=anyio.Event(),
            shutdown_event=anyio.Event(),
            provider=provider,
            user_count=1,
            status="pending",
            message="Plan scheduled",
            check_usable=check_usable,
        )
        self._pending_sub_shutdown_events.add(state.shutdown_event)

        # Add to active plans
        self._active_plans[plan] = state

        # Start plan worker
        self._task_group.start_soon(self._plan_worker, state)

        return state

    def _create_task_result_handle_provider(
        self, handle: core.TaskResultHandle | core.WorkflowStepResultHandle
    ) -> tuple[provider_base.ImplProvider, Callable[[], bool]]:
        """Create a provider and check_usable for a TaskResultHandle based on current state.

        Returns:
            Tuple of (provider, check_usable_callable)
        """
        if isinstance(handle, core.TaskResultHandle):
            task_cid = handle.task_ref
        elif isinstance(handle, core.WorkflowStepResultHandle):
            task_cid = handle.workflow_ref
        else:
            raise RuntimeError(f"Unsupported handle type: {type(handle).__name__}")

        # Use cached runset from launch
        assert self._runset_at_launch is not None
        runset = self._runset_at_launch

        # Check if task is in runset
        if task_cid in runset.runs:
            run_cid = runset.runs[task_cid]
            provider = RunsetTaskResultProvider(handle, run_cid, self._run_data_loader)
            # Check usable: run must still be in runset
            check_usable = lambda: provider.check_usable_against(self._runset_at_launch)
            return provider, check_usable

        # Check if task is currently active
        for task, task_state in self._active_tasks.items():
            if core.entity_cid(task) == task_cid:
                provider = ActiveTaskResultProvider(task_state, self._run_data_loader)
                # Check usable: task must still be runset-compatible
                check_usable = functools.partial(
                    self._verify_task_worker_is_runset_compatible, task_state
                )
                return provider, check_usable

        # Task not available
        raise RuntimeError(
            f"{_summarize(handle)} is not available. "
            "It must be either in the active runset or currently running."
        )

    def _create_workflow_result_handle_provider(
        self, handle: core.WorkflowResultHandle
    ) -> tuple[provider_base.ImplProvider, Callable[[], bool]]:
        """Create a provider and check_usable for a WorkflowResultHandle based on current state.

        Returns:
            Tuple of (provider, check_usable_callable)
        """
        assert isinstance(handle, core.WorkflowResultHandle)

        # Use cached runset from launch
        assert self._runset_at_launch is not None
        runset = self._runset_at_launch

        # Walk the runset to check if workflow is already complete
        current_ref = handle.workflow_ref
        workflow_path = []
        while current_ref in runset.runs:
            run_cid = runset.runs[current_ref]
            workflow_path.append((current_ref, run_cid))
            run = self._run_data_loader(run_cid)
            assert isinstance(run, core.Run)
            assert run.task == current_ref
            run_data = self._run_data_loader(run_cid)
            match run_data:
                case core.WorkflowResult():
                    provider = RunsetWorkflowResultProvider(
                        handle, workflow_path, self._run_data_loader
                    )
                    # Check usable: run must still be in runset
                    check_usable = lambda: provider.check_usable_against(self._runset_at_launch)
                    return provider, check_usable
                case core.WorkflowState(next_step=next_step):
                    current_ref = next_step.workflow_ref
                case _:
                    raise RuntimeError(
                        f"Unexpected workflow run result type: {type(run_data).__name__}"
                    )

        # Check if leaf workflow is currently active
        for workflow, workflow_state in self._active_workflows.items():
            if core.entity_cid(workflow) == current_ref:
                provider = ActiveWorkflowResultProvider(
                    handle, workflow_path, workflow_state, self._run_data_loader
                )
                # Check usable: workflow must still be runset-compatible
                check_usable = functools.partial(
                    self._verify_prefixed_workflow_worker_is_runset_compatible,
                    workflow_path,
                    workflow_state,
                )
                return provider, check_usable

        # Task not available
        raise RuntimeError(
            f"{_summarize(handle)} is not available. "
            "It must be either in the active runset or currently running."
        )

    async def _provide_coordinator_binding(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        """Provide implementation for a coordinator binding plan.

        This method is used as a callable provider for RuntimeCoordinatorBinding plans.
        It returns the implementation from the bound plan and builds provenance that
        includes all transitive dependencies of the bound plan.
        """
        assert isinstance(plan, core.ExternalServiceIdentifier)

        # This provider is only called by the backend runner itself, so provenance_lock
        # should never be provided
        if provenance_lock is not None:
            raise RuntimeError(
                "RuntimeCoordinator provider should not receive provenance_lock "
                "(only used internally by backend runner)"
            )

        # Get the binding
        binding = self._active_runtime_coordinator_bindings[plan]

        # Get the bound plan's implementation from dependencies
        bound_impl = dependencies[binding.plan]

        # Get all transitive deps of the bound plan
        all_bound_deps = plan_dep_util.plan_deps(binding.plan, recursive=True)

        # Build provenance set from all dep workers
        bound_provenances = {}
        for dep_plan in all_bound_deps:
            dep_state = self._active_plans[dep_plan]
            assert dep_state.plan_provenance is not None
            bound_provenances[core.entity_cid(dep_plan)] = dep_state.plan_provenance

        provenance = externals_provider.RuntimeCoordinatorProvenance(
            bound_plan=binding.plan,
            bound_provenances=core.ImplProvenanceSet(provenances=frozendict(bound_provenances)),
        )

        return bound_impl, provenance

    def _decrement_plan_user_count(self, state: PlanWorkerState) -> None:
        """Decrement user count and trigger shutdown if zero.

        Must be called with _state_lock held.
        """
        state.user_count -= 1
        if state.user_count == 0:
            state.shutdown_event.set()
            # Remove from active plans (don't move to stale - that's only for runset invalidation)
            if state.plan in self._active_plans:
                del self._active_plans[state.plan]

    # Worker coroutines

    async def _plan_worker(self, state: PlanWorkerState) -> None:
        """Worker coroutine that manages a plan's lifecycle."""
        try:
            # Wait for all dependencies
            dep_impls = {}
            for dep_state in state.dep_states.values():
                await dep_state.ready_event.wait()

                # Check if any deps failed/cancelled
                if dep_state.status in ("failed", "cancelled"):
                    state.status = "cancelled"
                    state.message = "Dependency cancelled"
                    state.ready_event.set()
                    self._publish_update()
                    await state.shutdown_event.wait()
                    return

                assert dep_state.impl is not None
                dep_impls[dep_state.plan] = dep_state.impl

            # Provide the plan
            state.status = "setup"
            state.message = "Setting up implementation"
            self._publish_update()

            impl, provenance = await state.provider.provide(state.plan, dep_impls, None)

            state.impl = impl
            state.plan_provenance = provenance
            state.status = "ready"
            state.message = "Implementation ready"
            state.ready_event.set()
            self._publish_update()

            # Wait for shutdown
            await state.shutdown_event.wait()

        except Exception as e:
            logger.exception(f"Plan worker failed for {_summarize(state.plan)}")
            state.status = "failed"
            state.message = f"Error: {e}"
            state.ready_event.set()
            self._publish_update()
        finally:
            # Always trigger shutdown event if not already set
            state.shutdown_event.set()
            self._pending_sub_shutdown_events.discard(state.shutdown_event)

            # Cleanup: decrement user counts of dependencies
            async with self._state_lock:
                for dep_state in state.dep_states.values():
                    self._decrement_plan_user_count(dep_state)
                # Remove from active plans or stale plans
                if state.plan in self._active_plans:
                    del self._active_plans[state.plan]
                elif id(state) in self._stale_plan_states:
                    del self._stale_plan_states[id(state)]

    async def _task_worker(self, state: TaskWorkerState) -> None:
        """Worker coroutine that manages a task's execution."""
        task = state.task
        task_cid = core.entity_cid(task)

        try:
            # Wait for task function to be ready
            state.status = "waiting"
            state.status_message = "Waiting for task function"
            self._publish_update()

            await state.task_function_state.ready_event.wait()

            if state.task_function_state.status != "ready":
                state.status = "cancelled"
                state.status_message = "Task function setup failed"
                state.done_event.set()
                self._publish_update()
                return

            # Pre-execution check: verify all run provenance is in runset
            # and this task is not yet in runset
            runset = api_lib.load_active_runset(self._config)
            if task_cid in runset.runs:
                state.status = "cancelled"
                state.status_message = "Task already in runset"
                state.done_event.set()
                self._publish_update()
                return

            # Verify all dependency runs are in runset
            all_deps = plan_dep_util.plan_deps(task.function, recursive=True)
            for dep in all_deps:
                if isinstance(dep, core.TaskResultHandle):
                    if dep.task_ref not in runset.runs:
                        state.status = "cancelled"
                        state.status_message = f"Dependency not in runset"
                        state.done_event.set()
                        self._publish_update()
                        return

            # Execute task
            state.status = "running"
            state.status_message = "Executing task"
            self._publish_update()

            task_impl = state.task_function_state.impl
            assert task_impl is not None
            assert core.implements_interface(task_impl, core.TaskFunction)
            result = await task_impl.run_task()

            # Build provenance
            provenance_record = core.ImplProvenanceSet(
                provenances=frozendict(
                    {
                        core.entity_cid(dep): state.all_dep_states[dep].plan_provenance
                        for dep in state.all_dep_states.keys()
                    }
                )
            )

            # Save results
            self._run_data_locator.save_entity(result)
            self._run_data_locator.save_entity(provenance_record)

            run_obj = core.Run(
                run_data=core.entity_cid(result),
                task=task_cid,
                provenance=core.entity_cid(provenance_record),
                run_info=core.BasicRunInfo(
                    author=self._config.author.name,
                    author_email=self._config.author.email,
                    timestamp=datetime.datetime.now().isoformat(),
                    message=state.run_message,
                    metadata=core.AdHocMetadata(metadata=frozendict({})),
                ),
            )
            self._run_data_locator.save_entity(run_obj)
            run_cid = core.entity_cid(run_obj)

            # Update state
            state.run_cid = run_cid
            state.status = "completed"
            state.status_message = "Task completed"

            # Save run state to disk
            task_run_state = api_lib.TaskRunState(
                run_name=datetime.datetime.now().isoformat(),
                state="completed",
                status_message="Task completed",
                run_cid=run_cid,
            )
            api_lib.save_run_state(self._config, task_cid, task_run_state)

            # Update runset (with conflict handling)
            await self._add_run_to_runset(task_cid, run_cid)

            state.done_event.set()
            self._publish_update()

        except Exception as e:
            logger.exception(f"Task worker failed for {_summarize(task)}")
            state.status = "failed"
            state.status_message = f"Error: {e}"

            # Save failed state
            task_run_state = api_lib.TaskRunState(
                run_name=datetime.datetime.now().isoformat(),
                state="failed",
                status_message=str(e),
                run_cid=None,
            )
            api_lib.save_run_state(self._config, task_cid, task_run_state)

            state.done_event.set()
            self._publish_update()
        finally:
            # Decrement task function user count
            async with self._state_lock:
                self._decrement_plan_user_count(state.task_function_state)
                # Remove from active tasks or stale tasks
                if task in self._active_tasks:
                    del self._active_tasks[task]
                elif id(state) in self._stale_task_states:
                    del self._stale_task_states[id(state)]

    async def _workflow_worker(self, state: FullWorkflowWorkerState) -> None:
        """Worker coroutine that manages a workflow's execution."""
        while True:
            try:
                state.status = "in_step"
                # Wait for first step task
                await state.current_step_state.done_event.wait()
                if state.current_step_state.status != "completed":
                    state.status = "cancelled"
                    state.done_event.set()
                    self._publish_update()
                    return

                # Either resolve or produce a new step.
                run_cid = state.current_step_state.run_cid
                assert run_cid is not None
                run = self._run_data_loader(run_cid)
                assert isinstance(run, core.Run)
                if run.run_data is None:
                    raise RuntimeError(f"Run {run_cid} has no run data")
                result = self._run_data_loader(run.run_data)
                if isinstance(result, core.WorkflowResult):
                    # Workflow is complete!
                    state.status = "done"
                    state.done_event.set()
                    self._publish_update()
                    return
                elif isinstance(result, core.WorkflowState):
                    if state.current_step_state in self._active_workflows:
                        assert id(state) not in self._stale_workflow_states
                        # Workflow is not complete. Launch the next step and update the worker
                        # states.

                        inner_tasks, final_workflow_task = _extract_tasks_from_workflow_state(
                            result
                        )
                        new_requests = []
                        for task in inner_tasks:
                            if isinstance(task, core.Task):
                                new_requests.append(task)
                            elif isinstance(task, core.Workflow):
                                new_requests.append(FullWorkflowLaunch(task))
                            else:
                                raise ValueError(f"Unsupported task type: {type(task).__name__}")
                        await self.launch(new_requests)
                        next_step_state = self._active_tasks[final_workflow_task]

                        next_wf_state = FullWorkflowWorkerState(
                            current_step=result.next,
                            current_step_state=next_step_state,
                            rest_of_workflow_state=None,
                            done_event=state.done_event,
                            status="pending",
                            run_message=state.run_message,
                        )
                        state.status = "in_rest"
                        state.rest_of_workflow_state = next_wf_state

                        # This worker task will now be in charge of the rest of the workflow.
                        del self._active_workflows[state.current_step]
                        self._active_workflows[next_wf_state.current_step] = next_wf_state
                        state = next_wf_state
                    else:
                        # This workflow is no longer active. Cancel it.
                        # TODO(daniel): is this the right thing to do?
                        state.status = "cancelled"
                        state.done_event.set()
                        self._publish_update()
                        return

                raise NotImplementedError("need to create a new state, remove old state")

            finally:
                if state.current_step_state in self._active_workflows:
                    del self._active_workflows[state.current_step]
                if id(state) in self._stale_workflow_states:
                    del self._stale_workflow_states[id(state)]

    async def _add_run_to_runset(self, task_cid: cbrrr.CID, run_cid: cbrrr.CID) -> None:
        """Add a run to the active runset with conflict handling."""
        try:
            runset = api_lib.load_active_runset(self._config)
            updated_runset = api_lib.add_run_to_runset(runset, task_cid, run_cid)
            api_lib.save_active_runset(self._config, updated_runset)
        except ValueError:
            # Conflict - run already exists or dependency issue
            # Still saved the run data, just not in runset
            logger.warning(f"Could not add run {run_cid} to runset due to conflict")

    def _publish_update(self) -> None:
        """Publish current state as an update."""
        update = dict(
            timestamp=datetime.datetime.now().isoformat(),
            plan_states={
                core.entity_cid(plan).encode("base32"): {
                    "status": state.status,
                    "message": state.message,
                    "user_count": state.user_count,
                }
                for plan, state in self._active_plans.items()
            },
            task_states={
                core.entity_cid(task).encode("base32"): {
                    "status": state.status,
                    "message": state.status_message,
                    "run_cid": state.run_cid.encode("base32") if state.run_cid else None,
                }
                for task, state in self._active_tasks.items()
            },
            workflow_states={
                core.entity_cid(workflow).encode("base32"): {
                    "status": state.status,
                }
                for workflow, state in self._active_workflows.items()
            },
        )
        self._update_publisher(update)


# Helper functions for creating/attaching to runners


@contextlib.asynccontextmanager
async def attach_to_runserver(
    config: fxtrconfig.FxtrConfig,
) -> AsyncGenerator[BackendRunnerInterface, None]:
    """Load runserver state and connect to it.

    Args:
        config: The fxtr configuration

    Yields:
        A BackendRunnerInterface connected to the remote runner

    Raises:
        RuntimeError: If no runserver is running
    """
    from ..impls import impl_remote

    runserver_state = api_lib.load_runserver_state(config)
    if runserver_state is None:
        raise RuntimeError(
            "No runserver is currently running. "
            "Start a runserver with 'fxtr runserver' or let create_or_attach_runner create one."
        )

    async with impl_remote.RemoteImpl.connect(
        interface_spec=core.spec_of(BackendRunnerInterface),
        base_url=runserver_state.runner_url,
    ) as remote_impl:
        # Verify protocol implementation
        assert core.implements_interface(remote_impl, BackendRunnerInterface)
        yield typing.cast(BackendRunnerInterface, remote_impl)


@contextlib.asynccontextmanager
async def create_or_attach_runner(
    config: fxtrconfig.FxtrConfig,
) -> AsyncGenerator[BackendRunnerInterface, None]:
    """Create a local runner or attach to existing runserver.

    This helper checks if a runserver is running and connects to it if so,
    otherwise creates a local BackendRunner instance.

    Note: The runner is NOT yet activated with a collection. The caller must
    call runner.activate_collection() before launching tasks.

    Args:
        config: The fxtr configuration

    Yields:
        A BackendRunnerInterface (local or remote), not yet activated
    """
    # Check if runserver exists
    runserver_state = api_lib.load_runserver_state(config)

    if runserver_state:
        # Attach to existing runserver
        async with attach_to_runserver(config) as runner:
            yield runner
    else:
        # Create local runner
        async with BackendRunner.create(config) as local_runner:
            yield local_runner
