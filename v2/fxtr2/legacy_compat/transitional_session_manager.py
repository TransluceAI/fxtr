"""Transitional session manager for old-fxtr on new-fxtr.

Partially derived from fxtr/fxtr/api/session_manager.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import typing
import weakref
from typing import Any, AsyncGenerator, Callable, Collection, Literal

import anyio
import anyio.abc
from frozendict import frozendict
from fxtr2.data import fxtr_context_api, fxtr_scope
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_serdes import EntityRef
from fxtr2.legacy_compat.data import LegacyCompat, UpgradeCache
from fxtr2.legacy_compat.transitional_rundata import (
    ImplProvenanceSet,
    RunWithPredecessors,
    TaskToRunMap,
    TaskWithPredecessors,
)
from fxtr2.util import anyio_util

from fxtr.api import api_lib, fxtrconfig
from fxtr.impl_plans import py_impl_plan
from fxtr.object_model import collection_tracking
from fxtr.object_model import core as v1_core
from fxtr.providers import externals_provider, in_process_py_provider, uv_py_provider

logger = logging.getLogger(__name__)


from fxtr.api.session_manager import (  # these were originally private but we can import them temporarily here to avoid redefining
    PlanManager,
    PlanWorker,
    ProviderBasedPlanWorker,
    RuntimeEnvironmentManager,
    _BackendRunnerPlanForwarder,
    _setup_uv_provider,
)

# endregion
####################################################################################################
# region: V2-based run loading
####################################################################################################


class TaskHandleFromRunCacheWorker(PlanWorker[Any]):
    """Worker for a task handle from a run cache.

    This worker serves a v1-fxr task handle by loading it from a v2-fxtr run stored in the a
    fxtr namespace.
    """

    def __init__(
        self,
        plan: v1_core.TaskResultHandle,
        manager: RunCacheManager,
    ):
        self.manager = manager
        super().__init__(plan=plan, dependency_workers={})

    async def _build_impl(
        self, dep_impls: dict[v1_core.Plan[Any], Any]
    ) -> tuple[Any, v1_core.AnyEntity]:
        """Build the implementation and provenance for this task handle worker."""
        assert isinstance(self.plan, v1_core.TaskResultHandle)

        # Get the v1 Task CID from the plan
        task_cid = self.plan.task_ref

        # Look up the TaskWithPredecessors via UpgradeCache (returns EntityRef)
        task_ref = await self.manager._upgrade_cache.lookup_upgraded(task_cid, TaskWithPredecessors)

        # Look up the RunWithPredecessors from the cache
        run = await self.manager.get_run_for_task(task_ref)
        if run is None:
            raise RuntimeError(
                f"Task {task_cid} has not been invoked yet. "
                "You must invoke_task() before instantiating plans that depend on it."
            )

        # Load the run data (ChildRef needs explicit loading)
        run_data_compat = await run.run_data.load()
        run_data_value = run_data_compat.value

        # Create the implementation (ConstantRetriever wrapping the value)
        impl = externals_provider.ConstantRetriever(run_data_value)

        # Create the provenance (RunProvenance with the legacy run CID)
        legacy_run_cid = run.legacy_run_entity.legacy_cid
        provenance = externals_provider.RunProvenance(run_cid=legacy_run_cid)

        return impl, provenance


# FxtrID data looks like "dXXXXXXXX..." and we want to take the first 2 non-constant chars as the shard
# (roughly 32 * 32 = 1024 shards).
FXTR_ID_DATA_SHARD_OFFSET = 3


class RunCacheManager:
    """Manager of the v2 run cache.

    - responsible for setting up workers to read from the cache
    - responsible for storing completed runs into the cache, and checks consistency at this time /
      walks deps and stores all previous deps into the cache
    - can check consistency of the cache

    maintains the run cache as a namespace mapping TaskWithPredecessors id string to
    RunWithPredecessors.
    """

    def __init__(
        self, run_cache_namespace: fxtr_context_api.FxtrNamespace, upgrade_cache: UpgradeCache
    ):
        """Initialize the run cache manager.

        Args:
            run_cache_namespace: The namespace to use for the run cache.
            upgrade_cache: The upgrade cache to use to upgrade legacy entities to v2 equivalents.
        """
        self._run_cache_namespace = run_cache_namespace
        self._upgrade_cache = upgrade_cache

    def make_task_handle_worker(
        self, plan: v1_core.TaskResultHandle
    ) -> TaskHandleFromRunCacheWorker:
        """Make a task handle worker for a task handle."""
        return TaskHandleFromRunCacheWorker(plan=plan, manager=self)

    def _get_pin_key(self, task_id: FxtrID) -> str:
        """Get the pin key for a task."""
        task_id_str = task_id.id_string()
        prefix = task_id_str[:FXTR_ID_DATA_SHARD_OFFSET]
        suffix = task_id_str[FXTR_ID_DATA_SHARD_OFFSET:]
        return f"shards/{prefix}:{suffix}"

    async def save_run(self, run: RunWithPredecessors[Any]) -> None:
        """Save a run to the cache.

        Before saving, checks that all predecessors of the run are also in the cache, and errors
        out if not.

        Args:
            run: The run to save.
        """
        # Check that all predecessor runs are in the cache with matching identity
        for predecessor_run_ref in run.previous_runs:
            # Load the predecessor run to get its task
            predecessor_run = await predecessor_run_ref.load()
            predecessor_task_id = predecessor_run.task.ref

            # Check that this exact run is in the cache for that task
            cached_run_ref = await self._run_cache_namespace.try_retrieve(
                self._get_pin_key(predecessor_task_id), type=RunWithPredecessors
            )
            if cached_run_ref is None:
                raise RuntimeError(
                    f"Cannot save run: predecessor run for task {predecessor_task_id.id_string()} "
                    "is not in cache. Predecessor runs must be saved before dependent runs."
                )

            # Verify it's the same run (by comparing legacy CIDs)
            if cached_run_ref != predecessor_run_ref:
                raise RuntimeError(
                    f"Cannot save run: predecessor run for task {predecessor_task_id.id_string()} "
                    f"has a different run in cache. Expected run with FxtrID "
                    f"{predecessor_run_ref.ref}, but found "
                    f"{cached_run_ref.ref}."
                )

        # Pin the run with the task's FxtrID as the key
        pin_key = self._get_pin_key(run.task.ref)
        await self._run_cache_namespace.pin(pin_key, run, overwrite=False)

    async def get_run_ref_for_task(
        self, task: TaskWithPredecessors[Any] | EntityRef[TaskWithPredecessors[Any]] | FxtrID
    ) -> EntityRef[RunWithPredecessors[Any]] | None:
        """Get a run for a task.

        Args:
            task: The task to get a run for. Can also be a reference to the task or its FxtrID.

        Returns:
            A reference to the run for the task, or None if no run is found.
        """
        # Extract the FxtrID from the input
        if isinstance(task, FxtrID):
            task_id = task
        elif isinstance(task, EntityRef):
            task_id = task.ref
        else:
            # It's a TaskWithPredecessors entity - store it to get the ID
            # (safe for immutable content-addressed entities)
            task_ref = task.store()
            task_id = task_ref.ref

        # Try to retrieve from the namespace
        pin_key = self._get_pin_key(task_id)
        run_ref = await self._run_cache_namespace.try_retrieve(pin_key, type=RunWithPredecessors)
        if run_ref is None:
            return None
        else:
            return run_ref

    async def get_run_for_task(
        self, task: TaskWithPredecessors[Any] | EntityRef[TaskWithPredecessors[Any]] | FxtrID
    ) -> RunWithPredecessors[Any] | None:
        """Get a run for a task.

        Args:
            task: The task to get a run for. Can also be a reference to the task or its FxtrID.

        Returns:
            The run for the task, or None if no run is found.
        """
        run_ref = await self.get_run_ref_for_task(task)
        if run_ref is None:
            return None
        else:
            return await run_ref.load()

    async def check_consistency(self) -> None:
        """Check the consistency of the cache.

        The cache is consistent if, for every pin in the cache namespace:
        - the value of the pin is a RunWithPredecessors
        - the task of the RunWithPredecessors is a TaskWithPredecessors whose FxtrID is the pin key
        - every predecessor of the RunWithPredecessors is also in the cache, at the location given
            by the FxtrID of its own TaskWithPredecessors

        Raises an exception if the cache is inconsistent.
        """
        async with fxtr_scope.FxtrScope(include=[self._run_cache_namespace.scope]):
            # List all pins in the namespace
            all_pins = await self._run_cache_namespace.list_pins()

            for pin_key, _pin_id in all_pins.items():
                # Load the pinned entity
                try:
                    run_ref = await self._run_cache_namespace.retrieve(
                        pin_key, type=RunWithPredecessors
                    )
                    run = await run_ref.load()
                except Exception as e:
                    raise RuntimeError(
                        f"Cache inconsistency: pin {pin_key!r} could not be loaded as "
                        f"RunWithPredecessors: {e}"
                    ) from e

                # Verify the task's FxtrID matches the pin key
                task_id_str = run.task.ref.id_string()
                expected_pin_key = self._get_pin_key(run.task.ref)
                if expected_pin_key != pin_key:
                    raise RuntimeError(
                        f"Cache inconsistency: pin key {pin_key!r} does not match task FxtrID "
                        f"{task_id_str!r} in the stored run (expected {expected_pin_key!r})."
                    )

                # Check all predecessor runs are in the cache at the correct location
                for predecessor_run_ref in run.previous_runs:
                    predecessor_run = await predecessor_run_ref.load()
                    predecessor_task_id = predecessor_run.task.ref
                    predecessor_pin_key = self._get_pin_key(predecessor_task_id)

                    # Verify the predecessor is pinned
                    if predecessor_pin_key not in all_pins:
                        raise RuntimeError(
                            f"Cache inconsistency: run at {pin_key!r} has predecessor run for task "
                            f"{predecessor_pin_key!r}, but that task has no run in the cache."
                        )

                    # Retrieve the cached predecessor and verify it's the same run
                    cached_predecessor_ref = await self._run_cache_namespace.retrieve(
                        predecessor_pin_key, type=RunWithPredecessors
                    )

                    if cached_predecessor_ref.ref != predecessor_run_ref:
                        raise RuntimeError(
                            f"Cache inconsistency: run at {pin_key!r} references predecessor run "
                            f"with FxtrID {predecessor_run_ref.ref}, but "
                            f"the cache has a different run at {predecessor_pin_key!r} with "
                            f"FxtrID {cached_predecessor_ref.ref}."
                        )


# endregion
####################################################################################################
# region: Adapted session manager
####################################################################################################


class TransitionalSessionManager:
    """Session manager that allows running v1-fxtr tasks backed by the v2 storage system.

    Will eventually be replaced by a native v2-fxtr session manager.

    Currently, only supports v1-fxtr Tasks, not Workflows.
    """

    def __init__(
        self,
        _task_group: anyio.abc.TaskGroup,
        _shutdown_event: anyio.Event,
        _python_provider: (
            uv_py_provider.UVManagedPyImplProvider | in_process_py_provider.InProcessPyProvider
        ),
        _run_cache_manager: RunCacheManager,
        _config: fxtrconfig.FxtrConfig,
    ):
        import weakref

        self._task_group = _task_group
        self._shutdown_event = _shutdown_event
        self._python_provider = _python_provider
        self._run_cache_manager = _run_cache_manager
        self._config = _config

        # Track tasks currently being invoked to prevent concurrent invocation
        self._currently_invoking: set[FxtrID] = set()

        # Create the plan manager with our worker builder
        weak_self = weakref.proxy(self)
        self._plan_manager = PlanManager(
            task_group=_task_group,
            shutdown_event=_shutdown_event,
            worker_builder=lambda plan: weak_self._make_worker(plan),
        )

        # Create the runtime environment manager
        self._runtime_env_manager = RuntimeEnvironmentManager(
            plan_manager=self._plan_manager,
            config=_config,
        )

    @classmethod
    @contextlib.asynccontextmanager
    async def create(
        cls,
        config: fxtrconfig.FxtrConfig,
        run_cache_namespace: fxtr_context_api.FxtrNamespace,
        upgrade_cache: UpgradeCache,
        *,
        in_process_python: bool = True,
    ) -> AsyncGenerator[TransitionalSessionManager, None]:
        """Create a transitional session manager.

        Args:
            config: The configuration to use for the session manager.
            run_cache_namespace: The namespace to use for the run cache.
            upgrade_cache: The upgrade cache to use to upgrade legacy entities to v2 equivalents.
            in_process_python: Whether to use in-process Python for the session manager.

        Returns:
            The transitional session manager.
        """
        shutdown_event = anyio.Event()
        async with contextlib.AsyncExitStack() as stack:
            task_group = await stack.enter_async_context(
                anyio_util.create_task_group(collapse_exc_group=True)
            )

            if in_process_python:
                python_provider = in_process_py_provider.InProcessPyProvider()
            else:
                python_provider = await stack.enter_async_context(_setup_uv_provider(config))

            run_cache_manager = RunCacheManager(
                run_cache_namespace=run_cache_namespace,
                upgrade_cache=upgrade_cache,
            )

            manager = cls(
                _task_group=task_group,
                _shutdown_event=shutdown_event,
                _python_provider=python_provider,
                _run_cache_manager=run_cache_manager,
                _config=config,
            )
            try:
                yield manager
            finally:
                shutdown_event.set()

    async def _make_worker(self, plan: v1_core.Plan[Any]) -> PlanWorker[Any]:
        """Make a new plan worker for the given plan."""
        match plan:
            case py_impl_plan.PyImplPlan():
                provider = self._python_provider

                # Fetch dependency workers
                dep_workers: dict[v1_core.Plan[Any], PlanWorker[Any]] = {}
                for dep_plan in plan.impl_deps:
                    dep_workers[dep_plan] = await self._plan_manager.request_plan(dep_plan)

                # Build the worker
                worker = ProviderBasedPlanWorker(
                    plan=plan, dependency_workers=dep_workers, provider=provider
                )

                # Decrement dependency user counts, since the worker now holds a reference to them
                for dep_worker in dep_workers.values():
                    dep_worker.decrement_user_count()

                return worker

            case v1_core.ExternalServiceIdentifier():
                # Route runtime environment plans to RuntimeEnvironmentManager
                worker = await self._runtime_env_manager.make_runtime_env_worker(plan)
                return worker

            case v1_core.ConstantHandle():
                provider = externals_provider.ConstantProvider()
                worker = ProviderBasedPlanWorker(
                    plan=plan, dependency_workers={}, provider=provider
                )
                return worker

            case v1_core.TaskResultHandle():
                # Use RunCacheManager for task result handles
                worker = self._run_cache_manager.make_task_handle_worker(plan)
                return worker

            case v1_core.PinnedResultHandle():
                raise NotImplementedError(
                    "PinnedResultHandle is not supported in the transitional session manager."
                )

            case v1_core.WorkflowStepResultHandle() | v1_core.WorkflowResultHandle():
                raise NotImplementedError(
                    "Workflow handles are not supported in the transitional session manager. "
                    "Only Task handles are supported."
                )

            case _:
                raise ValueError(f"Unsupported plan type: {type(plan)}")

    async def instantiate[T: v1_core.FxtrCallTarget](self, plan: v1_core.Plan[T]) -> T:
        impl, _ = await self.instantiate_with_provenance(plan)
        return impl

    async def instantiate_with_provenance[
        T: v1_core.FxtrCallTarget
    ](self, plan: v1_core.Plan[T]) -> tuple[T, dict[v1_core.Plan[Any], v1_core.AnyEntity]]:
        """Instantiate a plan and return both the implementation and transitive provenance.

        Args:
            plan: The plan to instantiate.

        Returns:
            Tuple of (implementation, transitive_provenance) where transitive_provenance
            is a dict mapping dependency plans to their provenance entities.
        """
        worker = await self._plan_manager.request_plan(plan)
        try:
            impl = await worker.retrieve()
            forwarder = _BackendRunnerPlanForwarder(impl)

            # Keep the worker alive as long as the forwarder is alive
            worker.increment_user_count()
            weakref.finalize(forwarder, worker.decrement_user_count)

            transitive_provenance = worker.get_transitive_provenance()
            return typing.cast(T, forwarder), transitive_provenance
        finally:
            worker.decrement_user_count()

    async def activate_collection(self, collection: collection_tracking.Collection | str) -> None:
        """Activate a collection and extract runtime/secret bindings from it.

        This method discovers all external service declarations, assignments, and bindings
        from the provided collection and its dependencies, validates that there are no
        conflicts, and caches them for use in subsequent instantiate() calls.

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

        # Update runtime environment manager with bindings
        self._runtime_env_manager.set_runtime_bindings(
            secret_declarations=secret_declarations,
            runtime_env_assignments=runtime_env_assignments,
            runtime_coordinator_bindings=runtime_coordinator_bindings,
        )

    async def get_run_for_task[
        T: v1_core.EntityBase
    ](self, task: TaskWithPredecessors[T] | EntityRef[TaskWithPredecessors[T]] | FxtrID) -> (
        RunWithPredecessors[T] | None
    ):
        """Lookup a run for a task.

        Args:
            task: The task to lookup a run for.

        Returns:
            The run for the task, or None if no run is found.
        """
        return await self._run_cache_manager.get_run_for_task(task)

    async def invoke_task[
        T: v1_core.EntityBase
    ](
        self,
        task: TaskWithPredecessors[T],
        cache_behavior: Literal["require_cached", "require_uncached", "auto"] = "auto",
    ) -> T:
        """Invoke a task or workflow, or retrieve it from the cache.

        After the task invocation completes, the result handle for the task will become available in
        the session manager. However, the task handle will NOT be available until that point; it is
        the caller's responsibility to sequence tasks by dependency order.

        Raises an exception if the task is already being invoked.

        Args:
            task: The task or workflow to invoke.
            cache_behavior: The behavior to use when retrieving the task from the cache.
                - "require_cached": Require the task to be cached, and just return the result without
                    running it. If not cached, raise an error.
                - "require_uncached": Require the task to be uncached, and always run the task. If the
                    task is cached, raise an error, since it is not possible to re-run a cached task
                    without rebuilding the cache first.
                - "auto": Return a cached result if available, otherwise run the task and cache the
                    result. This setting also enables workflow tasks to be partially cached, if they
                    did not previously complete.

        Returns:
            The result of the task or workflow. Does not include the provenance of the task; this should
            be inspected separately.
        """
        # Store the task to get its EntityRef for tracking
        task_ref = task.store()
        task_id = task_ref.ref

        # Check if already being invoked
        if task_id in self._currently_invoking:
            raise RuntimeError(
                f"Task {task_id.id_string()} is already being invoked. "
                "Concurrent invocations of the same task are not allowed."
            )

        # Check the cache
        cached_run = await self._run_cache_manager.get_run_for_task(task_id)

        if cache_behavior == "require_cached":
            if cached_run is None:
                raise RuntimeError(
                    f"Task {task_id.id_string()} is not in the cache, but require_cached was specified."
                )
            run_data_compat = await cached_run.run_data.load()
            return run_data_compat.value

        if cache_behavior == "require_uncached":
            if cached_run is not None:
                raise RuntimeError(
                    f"Task {task_id.id_string()} is already in the cache, but require_uncached was specified."
                )

        if cache_behavior == "auto" and cached_run is not None:
            run_data_compat = await cached_run.run_data.load()
            return run_data_compat.value

        # Run the task
        self._currently_invoking.add(task_id)
        try:
            run_ref = await self._invoke_task_inner(task, task_ref)
            run = await run_ref.load()
            run_data_compat = await run.run_data.load()
            return run_data_compat.value
        finally:
            self._currently_invoking.discard(task_id)

    async def _invoke_task_inner[
        T: v1_core.EntityBase
    ](
        self,
        task: TaskWithPredecessors[T],
        task_ref: EntityRef[TaskWithPredecessors[T]],
    ) -> EntityRef[RunWithPredecessors[T]]:
        """Core task invocation logic. Returns the stored RunWithPredecessors ref.

        This method assumes the task is already marked as currently invoking.
        """
        print(f"Invoking task {task_ref}...")

        # Get the v1 task and run it
        v1_task = task.legacy_task.value
        task_fn, transitive_provenance = await self.instantiate_with_provenance(v1_task.function)
        result: T = await task_fn.run_task()

        print(f"Task {task_ref} result: {v1_core.entity_cid(result)}")

        # Create the v1 ImplProvenanceSet from transitive provenance
        v1_provenance_dict: dict[v1_core.CID_Of[v1_core.Plan[Any]], v1_core.AnyEntity] = {}
        for plan, prov_entity in transitive_provenance.items():
            v1_provenance_dict[v1_core.entity_cid(plan)] = prov_entity
        v1_impl_provenance_set = v1_core.ImplProvenanceSet(
            provenances=frozendict(v1_provenance_dict)
        )

        # Create the v1 BasicRunInfo
        v1_run_info = v1_core.BasicRunInfo(
            author=self._config.author.name,
            author_email=self._config.author.email,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            message=None,
            metadata=v1_core.AdHocMetadata(metadata=frozendict()),
        )

        # Create the v1 Run
        v1_run = v1_core.Run(
            task=v1_core.entity_cid(v1_task),
            provenance=v1_core.entity_cid(v1_impl_provenance_set),
            run_data=v1_core.entity_cid(result),
            run_info=v1_run_info,
        )

        # Build index from v1 task CID to RunProvenance for quick lookup
        task_cid_to_run_provenance: dict[
            v1_core.CID_Of[v1_core.Task[Any]], externals_provider.RunProvenance
        ] = {}
        for plan, prov_entity in transitive_provenance.items():
            if isinstance(plan, v1_core.TaskResultHandle) and isinstance(
                prov_entity, externals_provider.RunProvenance
            ):
                task_cid_to_run_provenance[plan.task_ref] = prov_entity

        # Collect previous runs from task predecessors and verify they match provenance
        previous_runs: set[EntityRef[RunWithPredecessors[Any]]] = set()
        for predecessor_task_ref in task.predecessors:
            predecessor_run = await self._run_cache_manager.get_run_for_task(predecessor_task_ref)
            if predecessor_run is None:
                raise RuntimeError(
                    f"Predecessor task {predecessor_task_ref.ref.id_string()} has no run in cache. "
                    "This should not happen if tasks are invoked in dependency order."
                )

            # Verify the cached run matches the provenance
            predecessor_task = await predecessor_task_ref.load()
            predecessor_v1_task_cid = predecessor_task.legacy_task.legacy_cid
            matching_provenance = task_cid_to_run_provenance.get(predecessor_v1_task_cid)

            cached_run_cid = predecessor_run.legacy_run_entity.legacy_cid
            if matching_provenance is None:
                raise RuntimeError(
                    f"Predecessor run mismatch: cached run has CID {cached_run_cid}, "
                    f"but provenance does not reference any run. "
                    "This indicates the cache is inconsistent with the current task execution."
                )
            elif matching_provenance.run_cid != cached_run_cid:
                raise RuntimeError(
                    f"Predecessor run mismatch: cached run has CID {cached_run_cid}, "
                    f"but provenance references run CID {matching_provenance}. "
                    "This indicates the cache is inconsistent with the current task execution."
                )

            # Get an EntityRef to the predecessor run
            predecessor_run_ref = predecessor_run.store()
            previous_runs.add(predecessor_run_ref)

        # Create the v2 ImplProvenanceSet wrapper
        v2_impl_provenance_set = await ImplProvenanceSet.upgrade_from(
            v1_impl_provenance_set, self._run_cache_manager._upgrade_cache
        )

        # Create the RunWithPredecessors
        run_with_predecessors = RunWithPredecessors(
            task=task_ref,
            provenance=v2_impl_provenance_set.store(),
            run_data=LegacyCompat(result).store(),
            run_info=LegacyCompat(v1_run_info),
            previous_runs=frozenset(previous_runs),
            legacy_run_entity=LegacyCompat(v1_run),
        )
        run_ref = run_with_predecessors.store()
        await self._run_cache_manager._upgrade_cache.save_upgraded(run_with_predecessors)

        # Save to cache
        await self._run_cache_manager.save_run(run_with_predecessors)

        print(f"Saved run {run_ref} to cache.")
        return run_ref

    async def materialize_all_tasks(
        self,
        tasks: Collection[TaskWithPredecessors[Any]],
        include_predecessors: bool = False,
    ) -> TaskToRunMap:
        """Materialize a collection of tasks into a run set.

        Does a topological sort of the tasks and then launches them in order. Raises an exception
        if any of the tasks in this set are actively being invoked already.

        Args:
            tasks: The tasks to materialize. Tasks that already have runs in the cache will be skipped
                and we will re-use the runs from the cache.
            include_predecessors: Whether to also include all predecessors of the provided tasks,
                and materialize them as well.

        Returns:
            A run map containing the materialized tasks.
        """
        # Expand to include predecessors if requested
        if include_predecessors:
            all_tasks: set[TaskWithPredecessors[Any]] = set(tasks)
            to_expand = list(tasks)
            while to_expand:
                t = to_expand.pop()
                for pred_ref in t.predecessors:
                    pred = await pred_ref.load()
                    if pred not in all_tasks:
                        all_tasks.add(pred)
                        to_expand.append(pred)
            tasks = all_tasks

        # Store all tasks and build mappings
        task_to_ref: dict[TaskWithPredecessors[Any], EntityRef[TaskWithPredecessors[Any]]] = {}
        for t in tasks:
            ref = t.store()
            task_to_ref[t] = ref

        # Check none are already being invoked
        for t, ref in task_to_ref.items():
            if ref.ref in self._currently_invoking:
                raise RuntimeError(
                    f"Task {ref.ref.id_string()} is already being invoked. "
                    "Cannot materialize tasks that are already being invoked."
                )

        # Create completion events for each task
        task_events: dict[FxtrID, anyio.Event] = {
            ref.ref: anyio.Event() for ref in task_to_ref.values()
        }

        # Map to collect results: task ref -> run ref
        results: dict[EntityRef[TaskWithPredecessors[Any]], EntityRef[RunWithPredecessors[Any]]] = (
            {}
        )

        async def worker(t: TaskWithPredecessors[Any]) -> None:
            """Worker to invoke a single task."""
            task_ref = task_to_ref[t]
            task_id = task_ref.ref

            # Check if already cached, and exit immediately if so
            cached_run_ref = await self._run_cache_manager.get_run_ref_for_task(task_id)
            if cached_run_ref is not None:
                results[task_ref] = cached_run_ref
                task_events[task_id].set()
                return

            # For predecessors NOT in our task set, verify they have runs in the cache
            for pred_ref in t.predecessors:
                pred_id = pred_ref.ref
                if pred_id not in task_events:
                    # External predecessor - must already be in cache
                    ext_run_ref = await self._run_cache_manager.get_run_ref_for_task(pred_id)
                    if ext_run_ref is None:
                        raise RuntimeError(
                            f"External predecessor task {pred_id.id_string()} has no run in cache. "
                            "All external predecessors must be materialized first."
                        )

            # Mark this task as currently invoking
            if task_id in self._currently_invoking:
                raise RuntimeError(
                    f"Task {task_id.id_string()} is already being invoked. "
                    "Cannot materialize tasks that are already being invoked. (Likely race condition, failed sync check)"
                )
            self._currently_invoking.add(task_id)
            try:
                # Wait for all predecessors in our task set to complete
                for pred_ref in t.predecessors:
                    pred_id = pred_ref.ref
                    if pred_id in task_events:
                        await task_events[pred_id].wait()

                # Invoke the task
                run_ref = await self._invoke_task_inner(t, task_ref)
                results[task_ref] = run_ref

                # Signal completion
                task_events[task_id].set()
            finally:
                self._currently_invoking.discard(task_id)

        # Launch all workers concurrently
        async with anyio.create_task_group() as tg:
            for t in tasks:
                tg.start_soon(worker, t)

        # Build the result map (keyed by TaskWithPredecessors refs)
        assert len(results) == len(tasks)
        return TaskToRunMap(runs=frozendict(results))


# endregion
####################################################################################################
# region: Helpers
####################################################################################################


async def spawn_session_manager_in_asyncio_task(
    config: fxtrconfig.FxtrConfig,
    run_cache_namespace_factory: Callable[
        [], contextlib.AbstractAsyncContextManager[fxtr_context_api.FxtrNamespace]
    ],
    upgrade_cache_namespace_factory: Callable[
        [], contextlib.AbstractAsyncContextManager[fxtr_context_api.FxtrNamespace]
    ],
    *,
    in_process_python: bool = True,
) -> tuple[TransitionalSessionManager, asyncio.Task[None]]:
    """Spawn a TransitionalSessionManager within an asyncio task, returning the session manager and the task.

    This function ensures the TransitionalSessionManager instance is created and accessible
    by threading it out via a future. If session manager creation fails, the
    exception is propagated.

    This is a convenience method to make it easier to work with TransitionalSessionManager
    interactively in a Jupyter notebook or other interactive environment, without having to
    use a top-level context manager. It uses asyncio directly rather than anyio because anyio
    does not natively support tasks outside of a task group.

    Args:
        config: The configuration to use for the session manager.
        run_cache_namespace_factory: Factory function that returns an async context manager
            for the run cache namespace.
        upgrade_cache_namespace_factory: Factory function that returns an async context manager
            for the upgrade cache namespace.
        in_process_python: Whether to use in-process Python for the session manager.

    Returns:
        A tuple of (session_manager, background_task). The background task keeps the
        session manager alive; cancel it to shut down the session manager.
    """
    session_manager_fut: asyncio.Future[TransitionalSessionManager] = (
        asyncio.get_event_loop().create_future()
    )

    async def _go() -> None:
        try:
            async with (
                run_cache_namespace_factory() as run_cache_namespace,
                upgrade_cache_namespace_factory() as upgrade_cache_namespace,
                TransitionalSessionManager.create(
                    config=config,
                    run_cache_namespace=run_cache_namespace,
                    upgrade_cache=UpgradeCache(upgrade_cache_namespace),
                    in_process_python=in_process_python,
                ) as session_manager,
            ):
                session_manager_fut.set_result(session_manager)
                await anyio.sleep_forever()
        except Exception as exc:
            if not session_manager_fut.done():
                session_manager_fut.set_exception(exc)
            raise
        except BaseException as exc:
            if not session_manager_fut.done():
                session_manager_fut.set_exception(
                    RuntimeError(f"Session manager base exception: {exc}")
                )
            raise

    task = asyncio.create_task(_go())

    # Wait directly for the session manager before returning
    sm = await session_manager_fut
    return sm, task
