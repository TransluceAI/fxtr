"""Session management."""

from __future__ import annotations

import abc
import asyncio
import contextlib
import dataclasses
import datetime
import json
import logging
import os
import pathlib
import pickle
import traceback
import typing
import warnings
import weakref
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import partial
from os import cpu_count
from typing import Any, AsyncGenerator, Callable, Iterable, Literal, Mapping, Protocol

import anyio
import anyio.abc
import cbrrr
from frozendict import frozendict
from sqlitedict import SqliteDict
from tqdm.auto import tqdm

from fxtr.api.backend_runner import _summarize

from ..impl_plans import plan_dep_util, py_impl_plan
from ..object_model import collection_tracking, core, dag_util, locator
from ..providers import externals_provider, in_process_py_provider, provider_base, uv_py_provider
from . import api_lib, fxtrconfig

logger = logging.getLogger(__name__)

# endregion
####################################################################################################
# region: Plan worker management
####################################################################################################


class PlanWorker[T: core.FxtrCallTarget](abc.ABC):
    """Plan worker base class.

    Each plan worker manages a single plan and its implementation, and takes care of its lifecycle.
    Plan workers also depend on other plan workers, which provide the implementations that their
    own implementations need.

    Plan worker lifecycles pass through a few different states:
    - "pending": The plan worker has not yet started setting up.
    - "setup": The plan worker is setting up its implementation.
    - "ready": The plan worker has an implementation ready to provide.
    - "down": The plan worker is no longer running.

    Plan workers can also be either "current" or "stale". A plan worker is current if new users
    should be allowed to use its implementation. When it becomes stale, new users should no longer
    use this implementation, and should get a new one when they request it. Note that when a plan
    becomes stale, all of its dependents must also become stale.
    """

    def __init__(
        self,
        plan: core.Plan[T],
        dependency_workers: dict[core.Plan[Any], PlanWorker[Any]],
    ) -> None:
        """Initialize a plan worker.

        Args:
            plan: The plan that this worker is managing.
            dependency_workers: A mapping from dependency plans to their worker states. All of these
                workers will have their dependency user counts incremented by this worker, and
                this worker will be configured to mark itself stale when any of these workers
                become stale.
        """
        self.plan = plan
        self.dependency_workers = dependency_workers
        self.required_by: set[PlanWorker[Any]] = set()
        self.setup_done_event = anyio.Event()
        self.shutdown_event = anyio.Event()

        self.lifecycle_state: Literal["pending", "setup", "ready", "down"] = "pending"
        self.is_current: bool = True
        self.user_count: int = 1
        self.status_message: str = ""
        self.state_callbacks: list[Callable[[], None]] = []

        self.impl: T | None = None
        self.provenance: core.AnyEntity | None = None
        self.exception: Exception | None = None

        for dworker in dependency_workers.values():
            dworker.increment_user_count(self)

    def add_state_callback(self, callback: Callable[[], None]) -> None:
        """Add a state callback to the plan worker."""
        self.state_callbacks.append(callback)

    @abc.abstractmethod
    async def _build_impl(self, dep_impls: dict[core.Plan[Any], Any]) -> tuple[T, core.AnyEntity]:
        """Build the implementation and provenance for this plan worker, once deps are ready.

        Will only be called once per worker instance.
        """
        raise NotImplementedError("Subclasses must implement this method")

    def _state_update(self) -> None:
        current_str = "current" if self.is_current else "stale"
        logger.debug(
            f"Plan worker {id(self):x} for {_summarize(self.plan)}:"
            f" {self.lifecycle_state}[{current_str}] - {self.status_message}"
        )
        for callback in self.state_callbacks:
            callback()

    async def run_lifecycle(self):
        """Run the plan worker lifecycle in a task."""
        logger.debug(f"Running lifecycle for {_summarize(self.plan)}")
        try:
            self.lifecycle_state = "setup"
            self.status_message = "Waiting for dependencies"
            self._state_update()

            dep_impls = {}
            for dep_state in self.dependency_workers.values():
                dep_impls[dep_state.plan] = await dep_state.retrieve()
            self.status_message = "Building implementation"
            self.impl, self.provenance = await self._build_impl(dep_impls)

            self.lifecycle_state = "ready"
            self.status_message = "Implementation ready"
            self._state_update()

            self.setup_done_event.set()
            await self.shutdown_event.wait()
        except Exception as e:
            self.exception = e
            self.status_message = f"Error: {e}"
        else:
            self.status_message = "Shutting down"
        finally:
            self.lifecycle_state = "down"
            self.impl = None
            self.setup_done_event.set()

            for dworker in self.dependency_workers.values():
                dworker.decrement_user_count(self)
            self._state_update()

    async def retrieve(self) -> T:
        """Wait and retrieve the implementation for this plan worker."""
        logger.debug(f"Waiting on setup done event {_summarize(self.plan)}")
        await self.setup_done_event.wait()
        if self.exception is not None:
            raise self.exception
        assert self.impl is not None
        return self.impl

    def increment_user_count(self, required_by: PlanWorker[Any] | None = None) -> None:
        """Increment the user count for this plan worker."""
        self.user_count += 1
        if required_by is not None:
            self.required_by.add(required_by)

    def decrement_user_count(self, required_by: PlanWorker[Any] | None = None) -> None:
        """Decrement the user count for this plan worker."""
        self.user_count -= 1
        if required_by is not None:
            assert required_by in self.required_by
            self.required_by.remove(required_by)
        if self.user_count == 0:
            self.mark_stale()
            self.shutdown_event.set()

    def mark_stale(self) -> None:
        """Mark this plan worker as stale."""
        if self.is_current:
            self.is_current = False
            self._state_update()
            for downstream in self.required_by:
                downstream.mark_stale()

    def get_provenance(self) -> core.AnyEntity:
        """Get the provenance for this plan worker."""
        if self.provenance is None:
            raise RuntimeError("Provenance is not yet available")
        return self.provenance

    def get_transitive_provenance(self) -> dict[core.Plan[Any], core.AnyEntity]:
        """Get the provenance for this plan worker and all transitive dependencies of its plan.

        Only includes provenance for plans that are explicitly dependencies of this plan, does not
        include provenance for other workers that are not dependencies of this plan.
        """
        if self.provenance is None:
            raise RuntimeError("Provenance is not yet available")
        acc = {}
        self._walk_plan_transitive_provenance(set(), acc)
        return acc

    def _walk_plan_transitive_provenance(
        self, seen_ids: set[int], acc: dict[core.Plan[Any], core.AnyEntity]
    ) -> None:
        if id(self) in seen_ids:
            return
        if self.plan in acc:
            raise RuntimeError(
                f"Plan {core.entity_cid(self.plan)} has multiple workers! Should never happen."
            )
        assert self.provenance is not None
        acc[self.plan] = self.provenance
        seen_ids.add(id(self))
        plan_deps = plan_dep_util.plan_deps(self.plan, recursive=False)
        for subplan in plan_deps:
            assert subplan in self.dependency_workers
            dworker = self.dependency_workers[subplan]
            dworker._walk_plan_transitive_provenance(seen_ids, acc)


class WorkerBuilder(Protocol):
    async def __call__(self, plan: core.Plan[Any]) -> PlanWorker[Any]:
        """Build a new plan worker for the given plan.

        Args:
            plan: The plan to build a worker for.

        Returns:
            The built plan worker. The builder does NOT need to run the worker's lifecycle; the
            plan manager will do that on its own.
        """
        ...


class PlanManager:
    """Manager for plan workers and their lifecycles."""

    def __init__(
        self,
        task_group: anyio.abc.TaskGroup,
        shutdown_event: anyio.Event,
        worker_builder: WorkerBuilder,
    ):
        """Initialize a plan manager.

        Args:
            task_group: The task group to use for running workers.
            shutdown_event: The event to use to signal that the plan manager should shut down.
            worker_builder: The worker builder to use to build new workers.
        """
        self.task_group = task_group
        self.shutdown_event = shutdown_event
        self.active_workers: dict[core.Plan[Any], PlanWorker[Any]] = {}
        self.all_workers: dict[int, PlanWorker[Any]] = {}
        self.worker_builder = worker_builder

        self.state_callbacks: list[Callable[[], None]] = []
        self._pending_sub_shutdown_events: set[anyio.Event] = set()

        self.task_group.start_soon(self._cleanup_pending_sub_shutdown_events)

    async def _cleanup_pending_sub_shutdown_events(self) -> None:
        """Cleanup pending sub shutdown events."""
        await self.shutdown_event.wait()
        for event in self._pending_sub_shutdown_events:
            event.set()

    def add_state_callback(self, callback: Callable[[], None]) -> None:
        """Add a state callback to the plan manager."""
        self.state_callbacks.append(callback)

    def _state_update(self) -> None:
        for callback in self.state_callbacks:
            callback()

    async def request_plan(self, plan: core.Plan[Any]) -> PlanWorker[Any]:
        """Request a plan worker for the given plan.

        Requesting a plan always increments its user count. Users should be sure to decrement the
        user count once they no longer need the plan worker. (Note that plan workers also increment
        user counts of their dependencies, so it generally makes sense to request a plan, pass it
        as a dependency, then decrement the user count, since the caller itself no longer needs
        it and the dependent plan will manage its own user count.)
        """
        logger.debug(f"Requesting worker for {_summarize(plan)}")
        if plan in self.active_workers:
            worker = self.active_workers[plan]
            if worker.is_current:
                logger.debug(f"Worker for {_summarize(plan)} is current, incrementing user count")
                worker.increment_user_count()
                return worker
            else:
                warnings.warn(f"Plan {core.entity_cid(plan)} is stale, shouldn't happen")
                # Stale, need to make a new one
                del self.active_workers[plan]

        worker = await self.worker_builder(plan)
        self.active_workers[plan] = worker
        self.all_workers[id(worker)] = worker
        self._pending_sub_shutdown_events.add(worker.shutdown_event)
        self.task_group.start_soon(self._run_worker, worker)
        logger.debug(f"Scheduled worker for {_summarize(plan)}")
        return worker

    async def _run_worker(self, worker: PlanWorker[Any]) -> None:
        """Run the lifecycle of the given plan worker."""

        def _propagate_and_maybe_mark_stale():
            if not worker.is_current and self.active_workers.get(worker.plan) is worker:
                logger.debug(f"Cleaning up after {_summarize(worker.plan)}")
                del self.active_workers[worker.plan]
            self._state_update()

        try:
            worker.add_state_callback(_propagate_and_maybe_mark_stale)
            await worker.run_lifecycle()
        finally:
            if self.active_workers.get(worker.plan) is worker:
                del self.active_workers[worker.plan]
            if id(worker) in self.all_workers:
                del self.all_workers[id(worker)]
            self._pending_sub_shutdown_events.discard(worker.shutdown_event)


class ProviderBasedPlanWorker(PlanWorker[Any]):
    """Plan worker that uses a provider to build its implementation."""

    def __init__(
        self,
        plan: core.Plan[Any],
        dependency_workers: dict[core.Plan[Any], PlanWorker[Any]],
        provider: provider_base.ImplProvider,
    ):
        self.provider = provider
        super().__init__(plan=plan, dependency_workers=dependency_workers)

    async def _build_impl(self, dep_impls: dict[core.Plan[Any], Any]) -> tuple[Any, core.AnyEntity]:
        """Build the implementation and provenance for this plan worker, once deps are ready."""
        impl, provenance = await self.provider.provide(self.plan, dep_impls, None)
        return impl, provenance


# endregion
####################################################################################################
# region: Active run management
####################################################################################################


@dataclasses.dataclass
class RunReservation:
    """Reservation for a single run.

    A run reservation reserves a slot in a runset for a task that has been scheduled to run. Run
    reservations can be fulfilled with a run, or cancelled if the task cannot actually execute.

    Run reservations are tracked by the active run manager as if they were completed runs. It is
    possible to request a storage handle for a run reservation even before the task completes, in
    which case the request will block until the reservation is either fulfilled or cancelled.

    Attributes:
        task: The task that the reservation is for.
        fulfilled_or_cancelled_event: An event that is set when the reservation is either fulfilled
            or cancelled.
        status_message: A message describing the status of the reservation.
    """

    task: core.Task | core.Workflow
    fulfilled_or_cancelled_event: anyio.Event
    status_message: str


@dataclasses.dataclass
class RunNode:
    """A node in the active run graph.

    Run nodes track the current state of the active (completed or pending) runs for each task or
    workflow in the system, as well as the dependencies between runs.

    We maintain an invariant that if X has Y as a dependency, then Y has X as a dependent. This
    allows us to walk between nodes and their dependents when removing them from the runset.
    Similarly, we maintain an invariant that if X has a worker W, then W is also tracking X, and
    when W becomes stale, it will remove itself from the node's workers.

    TODO: this introduces some unnecessary cyclical references. There is likely a simpler way to
    design this that ensures dependencies are appropriately tracked.

    Attributes:
        task_cid: The CID of the task or workflow that the node is for.
        run_or_reservation: The run or reservation that the node tracks. If a RunReservation, it
            may later be replaced with a completed run.
        dependencies: The dependencies of the run (keyed by python ID). May be modified if this
            node depends on a workflow reservation and that workflow reservation completes.
        dependents: The dependents of the run (keyed by python ID). Will be modified whenever a new
            node is added that depends on this one.
        workers: The workers that are currently using this node (keyed by python ID). Will be modified
            whenever a new worker is added that uses this node.
        workflow_contains: The tasks that the run contains (if it is a completed workflow run).
        workflow_next: The next step of the workflow (if it is a completed workflow run with a next
            step).
    """

    task_cid: core.CID_Of[core.Task | core.Workflow]
    run_or_reservation: core.Run | RunReservation
    dependencies: dict[int, RunNode | WorkflowCommitmentNode]
    dependents: dict[int, RunNode]
    workers: dict[int, PlanWorker[Any]]
    workflow_contains: set[core.CID_Of[core.Task | core.Workflow]] | None
    workflow_next: core.CID_Of[core.Workflow] | None
    # Whether `dependencies`/`dependents` are fully populated. Eagerly-built and in-session nodes
    # are edge-complete (True). A node loaded lazily from the cache starts as a stub (False) with
    # empty edge dicts until `_ensure_edges` wires its 1-hop neighborhood. (Used by lazy
    # loading; always True in the eager path.)
    edges_loaded: bool = True


@dataclasses.dataclass
class WorkflowCommitmentNode:
    """A node that tracks a workflow commitment.

    Workflow commitments are commitments to run all of the steps of a given workflow until it is
    complete, starting from a particular run reservation. More specifically, a workflow commitment
    for a given task is a promise that, if necessary, this task will be executed, and when that task
    is complete, if it resolves to a new workflow step, then that step will also have its own
    workflow commitment. Indirectly, this then means that a workflow commitment represents a
    promise to (promise to (promise to ...)) run a workflow until completion.

    The purpose of a workflow commitment is to identify which workflows are currently being
    executed, such that plans that depend on the workflow's result can be optimistically scheduled
    without blocking forever, and can be signalled when the workflow has made progress.

    Attributes:
        workflow_cid: The CID of the workflow that the commitment is for.
        advanced_or_cancelled_event: An event that is set when the commitment has advanced to a new
            workflow step. The expectation is that, when this event is set, the workflow should
            now have a run in the active set, and either that run has resolved to a final result
            or it has resolved to a new workflow step where that workflow step already has a new
            workflow commitment. (If set early, this means this workflow commitment was cancelled,
            and it is appropriate to also cancel plans that depend on this commitment.)
        dependents: The dependents of the commitment (keyed by python ID). Will be modified whenever
            a new node is added that depends on this one.
        succeeded: Whether the commitment was marked as successful. If None, the commitment is
            still active.
        status_message: A message describing the status of the commitment.
    """

    workflow_cid: core.CID_Of[core.Workflow]
    advanced_or_cancelled_event: anyio.Event
    dependents: dict[int, RunNode]
    succeeded: bool | None = None
    status_message: str = ""


def _walk_all_tasks_in_object(target: core.AnyFrozenIPLDable) -> set[core.Task | core.Workflow]:
    """Walk all tasks in the given workflow step data."""
    collector = dag_util.RecursiveSubgraphCollector(
        include_if=lambda ent: not isinstance(ent, (core.Task, core.Workflow))
    )
    collector.visit_part(spec=core.TypeSpec_AnyFrozenIPLDable(), value=target)
    tasks_and_workflows = set[core.Task | core.Workflow](
        typing.cast(list[core.Task | core.Workflow], collector.excluded_entities)
    )
    return tasks_and_workflows


def _topologically_sort_tasks(
    tasks: set[core.Task | core.Workflow],
) -> list[core.Task | core.Workflow]:
    """Topologically sort tasks and workflows by their dependencies.

    Args:
        tasks: Set of tasks and workflows to sort.

    Returns:
        List of tasks and workflows sorted in dependency order (dependencies first).
    """
    # Build mapping from CID to task
    cid_to_task: dict[cbrrr.CID, core.Task | core.Workflow] = {}
    task_to_remaining_deps: dict[core.Task | core.Workflow, set[core.Task | core.Workflow]] = {}
    dep_to_requestors: dict[core.Task | core.Workflow, set[core.Task | core.Workflow]] = {}

    for task_or_workflow in tasks:
        cid_to_task[core.entity_cid(task_or_workflow)] = task_or_workflow
        dep_to_requestors[task_or_workflow] = set()

    # Build dependency graph
    ready = []
    for task_or_workflow in cid_to_task.values():
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

    # Topological sort
    result = []
    while ready:
        task = ready.pop()
        result.append(task)
        for later in dep_to_requestors[task]:
            task_to_remaining_deps[later].remove(task)
            if len(task_to_remaining_deps[later]) == 0:
                ready.append(later)
        del dep_to_requestors[task], task_to_remaining_deps[task]

    return result


# ----------------------------------------------------------------------------------------------
# Cold run-graph cache build (fast, resumable, syncable). See notes/2026_06_15_durable_cache_pt2.md.
#
# Three stores: source (read-only runset), staging (temp; the expensive per-node work, checkpointed)
# and dest (a fresh, byte-compatible active.sqlite: runset rows + `_rg/` cache rows). Two stages:
#   1. source -> staging: parallel, independent-per-node extraction (no provenance). The expensive
#      phase, checkpointed so a crash never redoes it.
#   2. staging -> dest: a serial depth-first post-order walk that writes each dest row complete and
#      maintains the reverse index surgically (no in-memory invert). Local and cheap.
# Both stages skip work already present in their output store, so the whole thing is resumable and
# re-runnable as a sync (it picks up runs *added* to a monotonic source).
# ----------------------------------------------------------------------------------------------

# Commit every this many records (staging writes / dest builds). Bounds memory and crash-redo;
# resumability comes from skip-present, not from this.
_CACHE_BUILD_BATCH_SIZE = 20_000
# How many nodes a single extraction task handles (amortizes executor / IPC overhead).
_CACHE_BUILD_CHUNK_SIZE = 256
# How long a build-store op waits on a locked database before erroring.
_CACHE_BUILD_BUSY_TIMEOUT_MS = 30_000


def _open_build_store(path: pathlib.Path, *, autocommit: bool) -> SqliteDict:
    """Open a build store (staging or dest) with a busy timeout set."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = SqliteDict(str(path), autocommit=autocommit)
    conn = getattr(db, "conn", None)
    if conn is not None:
        try:
            conn.execute(f"PRAGMA busy_timeout = {_CACHE_BUILD_BUSY_TIMEOUT_MS}")
        except Exception:
            pass
    return db


def _workflow_contains_and_next(
    run: core.Run, loader: locator.CachingEntityLoader
) -> tuple[set[core.CID_Of[core.Task | core.Workflow]] | None, core.CID_Of[core.Workflow] | None]:
    """The (workflow_contains, workflow_next) for a run, or (None, None) if it is not a step."""
    run_data = loader(run.run_data)
    match run_data:
        case core.WorkflowState(step_data=step_data, next=next_wf):
            workflow_next = core.entity_cid(next_wf)
            workflow_contains = set(
                core.entity_cid(subtask) for subtask in _walk_all_tasks_in_object(step_data)
            ) | {workflow_next}
            return workflow_contains, workflow_next
        case core.WorkflowResult():
            return None, None
        case _:
            assert False, f"Unexpected workflow state type: {type(run_data)}"


def _extract_staging_record(
    loader: locator.CachingEntityLoader,
    task_cid: core.CID_Of[core.Task | core.Workflow],
    run_cid: core.CID_Of[core.Run],
) -> dict:
    """Load a node and compute its staging record.

    Pure per-node — touches no other node's state — so it is embarrassingly parallel (and safe to
    run in a separate process). `WorkflowResultHandle` chains are left *unresolved* here
    (`wf_result_refs`); stage 2 resolves them from staged `workflow_next` values. No provenance.
    """
    run = loader(run_cid)
    assert isinstance(run, core.Run)
    task = loader(run.task)
    assert isinstance(task, (core.Task, core.Workflow))
    direct_dep_cids: list[core.CID_Of[core.Task | core.Workflow]] = []
    wf_result_refs: list[core.CID_Of[core.Workflow]] = []
    for dep in plan_dep_util.plan_deps(task.function, recursive=True):
        match dep:
            case core.TaskResultHandle(task_ref=ref) | core.WorkflowStepResultHandle(
                workflow_ref=ref
            ):
                direct_dep_cids.append(ref)
            case core.WorkflowResultHandle(workflow_ref=ref):
                wf_result_refs.append(ref)
            case _:
                pass
    if isinstance(task, core.Workflow):
        workflow_contains, workflow_next = _workflow_contains_and_next(run, loader)
    else:
        workflow_contains, workflow_next = None, None
    return {
        "run": run,
        "run_cid": run_cid,
        "direct_dep_cids": direct_dep_cids,
        "wf_result_refs": wf_result_refs,
        "workflow_contains": list(workflow_contains) if workflow_contains is not None else None,
        "workflow_next": workflow_next,
    }


def _extract_chunk(
    loader: locator.CachingEntityLoader,
    chunk: list[tuple[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Run]]],
) -> list[tuple[str, core.CID_Of[core.Task | core.Workflow], object]]:
    """Extract a chunk of nodes; per node yield ("ok", cid, record) or ("fail", cid, error)."""
    out: list[tuple[str, core.CID_Of[core.Task | core.Workflow], object]] = []
    for task_cid, run_cid in chunk:
        try:
            out.append(("ok", task_cid, _extract_staging_record(loader, task_cid, run_cid)))
        except Exception as e:  # reported (skip) or aborted by the driver
            out.append(("fail", task_cid, repr(e)))
    return out


# Per-process loader for the ProcessPoolExecutor stage-1 workers (set by the initializer).
_BUILD_WORKER_LOADER: locator.CachingEntityLoader | None = None


def _build_worker_init(loc: locator.Locator) -> None:
    global _BUILD_WORKER_LOADER
    _BUILD_WORKER_LOADER = locator.CachingEntityLoader.from_locator(loc)


def _build_worker_extract_chunk(
    chunk: list[tuple[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Run]]],
) -> list[tuple[str, core.CID_Of[core.Task | core.Workflow], object]]:
    """ProcessPool worker: extract a chunk using the per-process loader."""
    assert _BUILD_WORKER_LOADER is not None, "build worker loader not initialized"
    return _extract_chunk(_BUILD_WORKER_LOADER, chunk)


class RunGraphCacheBuilder:
    """Builds the durable run-graph cache for a runset into a fresh destination store.

    Reads a read-only source runset (`task_cid -> run_cid`) + the entity store and produces a
    byte-compatible `active.sqlite` (runset rows + `_rg/` cache rows) in `dest_path`, using
    `staging_path` as a resumable checkpoint of the expensive per-node extraction. See the module
    comment above and notes/2026_06_15_durable_cache_pt2.md.

    Assumes the source is only *added to* during a build (monotonic). Re-running is an idempotent
    sync: both stages skip work already present in their output store.
    """

    def __init__(
        self,
        *,
        source_runs: Mapping[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Run]],
        loader: locator.CachingEntityLoader,
        staging_path: pathlib.Path,
        dest_path: pathlib.Path,
        copy_runset: bool = True,
        batch_size: int = _CACHE_BUILD_BATCH_SIZE,
        chunk_size: int = _CACHE_BUILD_CHUNK_SIZE,
        max_workers: int | None = None,
        use_processes: bool = False,
        skip_unbuildable: bool = False,
        keep_staging: bool = False,
    ) -> None:
        self.source_runs = dict(source_runs)
        self.loader = loader
        self.staging_path = staging_path
        self.dest_path = dest_path
        self.copy_runset = copy_runset
        self.batch_size = batch_size
        self.chunk_size = chunk_size
        self.use_processes = use_processes
        self.skip_unbuildable = skip_unbuildable
        # When True, keep the staging store after a successful run so a later run can extract only
        # *newly-added* source nodes (incremental sync) instead of re-extracting everything.
        self.keep_staging = keep_staging
        if max_workers is not None:
            self.max_workers = max_workers
        elif use_processes:
            self.max_workers = max(1, (cpu_count() or 1) - 1)
        else:
            self.max_workers = min(32, (cpu_count() or 1) + 4)
        # cids that failed to extract in stage 1 (only populated with skip_unbuildable).
        self._failed: set[core.CID_Of[core.Task | core.Workflow]] = set()
        # cids skipped in stage 2 (failed nodes + their transitive dependent closure).
        self._skipped: set[core.CID_Of[core.Task | core.Workflow]] = set()

    @staticmethod
    def _runset_key(cid: core.CID_Of[core.Task | core.Workflow]) -> str:
        return cid.encode(base="base32")

    @staticmethod
    def _cache_key(cid: core.CID_Of[core.Task | core.Workflow]) -> str:
        return api_lib.RUN_GRAPH_CACHE_KEY_PREFIX + cid.encode(base="base32")

    def run(self) -> tuple[int, int]:
        """Build the cache. Returns (built_count, skipped_count).

        Deletes the staging store on success unless `keep_staging` is set (keep it to make a later
        run extract only newly-added source nodes — an incremental sync — rather than everything).
        """
        self._run_stage1()
        built = self._run_stage2()
        if not self.keep_staging:
            self._delete_staging()
        return built, len(self._skipped)

    # -- stage 1: source -> staging (parallel) ----------------------------------------------------

    def _staging_cids(self) -> set[core.CID_Of[core.Task | core.Workflow]]:
        if not self.staging_path.exists():
            return set()
        with _open_build_store(self.staging_path, autocommit=True) as db:
            return set(cbrrr.CID.decode(k) for k in db.keys())

    def _run_stage1(self) -> None:
        logger.info(f"Stage 1: scanning staging ({len(self.source_runs)} source runs) ...")
        already = self._staging_cids()
        todo = [(c, r) for c, r in self.source_runs.items() if c not in already]
        if not todo:
            logger.info(f"Stage 1: all {len(self.source_runs)} nodes already staged; skipping.")
            return
        chunks = [todo[i : i + self.chunk_size] for i in range(0, len(todo), self.chunk_size)]
        logger.info(
            f"Stage 1: extracting {len(todo)} nodes ({len(already)} already staged) with"
            f" {self.max_workers} {'processes' if self.use_processes else 'threads'}."
        )
        buffer: list[tuple[core.CID_Of[core.Task | core.Workflow], dict]] = []
        if self.use_processes:
            executor = ProcessPoolExecutor(
                max_workers=self.max_workers,
                initializer=_build_worker_init,
                initargs=(self.loader.locator,),
            )
            results = executor.map(_build_worker_extract_chunk, chunks)
        else:
            executor = ThreadPoolExecutor(max_workers=self.max_workers)
            results = executor.map(partial(_extract_chunk, self.loader), chunks)
        try:
            with tqdm(
                total=len(todo), desc="Stage 1: extract", mininterval=0.5, smoothing=0
            ) as pbar:
                for chunk_result in results:
                    for status, cid, payload in chunk_result:
                        if status == "ok":
                            assert isinstance(payload, dict)
                            buffer.append((cid, payload))
                            if len(buffer) >= self.batch_size:
                                self._flush_staging(buffer)
                                buffer = []
                        elif self.skip_unbuildable:
                            self._failed.add(cid)
                            logger.warning(f"Stage 1: skipping unbuildable node {cid}: {payload}")
                        else:
                            raise RuntimeError(
                                f"Failed to extract node {cid} during run-graph cache build:"
                                f" {payload}. Re-run after repairing the entity store, or pass"
                                " skip_unbuildable to drop it and its transitive dependents."
                            )
                        pbar.update(1)
                if buffer:
                    self._flush_staging(buffer)
        finally:
            executor.shutdown()

    def _flush_staging(
        self, buffer: list[tuple[core.CID_Of[core.Task | core.Workflow], dict]]
    ) -> None:
        with _open_build_store(self.staging_path, autocommit=False) as db:
            for cid, record in buffer:
                db[self._runset_key(cid)] = record
            db.commit()

    # -- stage 2: staging -> dest (serial DAG-walk) -----------------------------------------------

    def _dest_built_cids(self) -> set[core.CID_Of[core.Task | core.Workflow]]:
        prefix = api_lib.RUN_GRAPH_CACHE_KEY_PREFIX
        if not self.dest_path.exists():
            return set()
        with _open_build_store(self.dest_path, autocommit=True) as db:
            return set(
                cbrrr.CID.decode(k[len(prefix) :]) for k in db.keys() if k.startswith(prefix)
            )

    def _run_stage2(self) -> int:
        logger.info("Stage 2: scanning staging/dest ...")
        built = self._dest_built_cids()
        n_start = len(built)
        with _open_build_store(self.staging_path, autocommit=True) as staging:
            all_cids = [cbrrr.CID.decode(k) for k in staging.keys()]
            logger.info(
                f"Stage 2: building {max(0, len(all_cids) - n_start)} nodes into dest"
                f" ({n_start} already built)."
            )
            rec_cache: dict[core.CID_Of[core.Task | core.Workflow], dict | None] = {}

            def read(cid: core.CID_Of[core.Task | core.Workflow]) -> dict | None:
                if cid not in rec_cache:
                    rec_cache[cid] = staging.get(self._runset_key(cid))
                return rec_cache[cid]

            buffer: list[tuple[core.CID_Of[core.Task | core.Workflow], dict, list]] = []
            with tqdm(
                total=max(0, len(all_cids) - n_start),
                desc="Stage 2: build",
                mininterval=0.5,
                smoothing=0,
            ) as pbar:
                for root in all_cids:
                    if root in built or root in self._skipped:
                        continue
                    stack: list[tuple[core.CID_Of[core.Task | core.Workflow], bool]] = [
                        (root, False)
                    ]
                    while stack:
                        cid, deps_done = stack[-1]
                        if cid in built or cid in self._skipped:
                            stack.pop()
                            continue
                        rec = read(cid)
                        if rec is None:
                            # Not staged: a failed/missing node (only reachable with
                            # skip_unbuildable; abort mode would have already raised in stage 1).
                            self._skipped.add(cid)
                            stack.pop()
                            continue
                        deps = self._resolve_deps(rec, read)
                        if not deps_done:
                            stack[-1] = (cid, True)
                            for d in deps:
                                if d not in built and d not in self._skipped:
                                    stack.append((d, False))
                            continue
                        # All deps have been attempted. If any was skipped, this node must be too
                        # (keep the kept set dependency-closed — never an edge-drop).
                        if any(d in self._skipped for d in deps):
                            self._skipped.add(cid)
                            stack.pop()
                            continue
                        buffer.append((cid, rec, deps))
                        built.add(cid)
                        rec_cache.pop(cid, None)
                        stack.pop()
                        pbar.update(1)
                        if len(buffer) >= self.batch_size:
                            self._flush_dest(buffer)
                            buffer = []
                if buffer:
                    self._flush_dest(buffer)
        return len(built) - n_start

    def _resolve_deps(self, rec: dict, read) -> list[core.CID_Of[core.Task | core.Workflow]]:
        """Full dependency_cids: direct handle deps + resolved WorkflowResultHandle chains (walked
        via staged workflow_next), de-duplicated and order-preserving."""
        deps = list(rec["direct_dep_cids"])
        for ref in rec["wf_result_refs"]:
            cur = ref
            guard = 0
            while cur is not None:
                deps.append(cur)
                step = read(cur)
                if step is None:
                    break
                cur = step["workflow_next"]
                guard += 1
                if guard > len(self.source_runs):
                    break
        seen: set[core.CID_Of[core.Task | core.Workflow]] = set()
        out: list[core.CID_Of[core.Task | core.Workflow]] = []
        for d in deps:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out

    def _flush_dest(
        self, buffer: list[tuple[core.CID_Of[core.Task | core.Workflow], dict, list]]
    ) -> None:
        additions: dict[core.CID_Of[core.Task | core.Workflow], list] = defaultdict(list)
        with _open_build_store(self.dest_path, autocommit=False) as db:
            # Own rows first, so a parent built in this same batch already has a row to append to.
            for cid, rec, deps in buffer:
                if self.copy_runset:
                    db[self._runset_key(cid)] = bytes(rec["run_cid"])
                db[self._cache_key(cid)] = {
                    "run": rec["run"],
                    "dependency_cids": deps,
                    "dependent_cids": [],
                    "workflow_contains": rec["workflow_contains"],
                    "workflow_next": rec["workflow_next"],
                }
                for d in deps:
                    additions[d].append(cid)
            # Surgical reverse index, grouped per parent (one read-modify-write per parent).
            for parent, children in additions.items():
                key = self._cache_key(parent)
                prec = db.get(key)
                if prec is None:
                    continue  # parent skipped (then its children are skipped too); defensive
                existing = prec["dependent_cids"]
                existing_set = set(existing)
                new = [c for c in children if c not in existing_set]
                if new:
                    prec["dependent_cids"] = existing + new
                    db[key] = prec
            db.commit()

    def _delete_staging(self) -> None:
        try:
            if self.staging_path.exists():
                self.staging_path.unlink()
        except Exception as e:
            logger.warning(f"Failed to delete staging store {self.staging_path}: {e}")


class TaskHandleWorker(PlanWorker[Any]):
    """Worker for a task handle."""

    def __init__(
        self,
        plan: core.TaskResultHandle | core.WorkflowStepResultHandle | core.WorkflowResultHandle,
        manager: ActiveRunManager,
    ):
        self.manager = manager
        self.bound_to_node: bool = False
        self.nodes: list[RunNode] = []
        super().__init__(plan=plan, dependency_workers={})

    def mark_stale(self) -> None:
        if self.is_current:
            for node in self.nodes:
                del node.workers[id(self)]
            self.nodes = []
        super().mark_stale()

    async def _build_impl(self, dep_impls: dict[core.Plan[Any], Any]) -> tuple[Any, core.AnyEntity]:
        """Build the implementation and provenance for this task handle worker."""
        assert not self.bound_to_node
        self.bound_to_node = True
        assert not dep_impls, "Task handle workers should not have dependencies"
        match self.plan:
            case core.TaskResultHandle(task_ref=task_ref) | core.WorkflowStepResultHandle(
                workflow_ref=task_ref
            ):
                if not self.is_current:
                    raise RuntimeError(
                        f"Task handle worker for {_summarize(self.plan)} is stale; not loading"
                        " stale task handles"
                    )
                # Task handle workers attach themselves to the plan
                node = self.manager._get_node(task_ref)
                if node is None:
                    raise RuntimeError(f"Task {task_ref} is not in the active set")

                # Track this node with this worker so that we are notified when the node is dropped.
                node.workers[id(self)] = self
                self.nodes.append(node)

                if isinstance(node.run_or_reservation, RunReservation):
                    res = node.run_or_reservation
                    await res.fulfilled_or_cancelled_event.wait()
                    if not isinstance(node.run_or_reservation, core.Run):
                        raise RuntimeError(
                            f"Reservation for task {task_ref} was not fulfilled (status:"
                            f" {res.status_message})"
                        )

                assert isinstance(node.run_or_reservation, core.Run)
                run = node.run_or_reservation
                provenance = externals_provider.RunProvenance(run_cid=core.entity_cid(run))
                return (
                    externals_provider.RunDataRetriever(
                        _run_data_cid=run.run_data,
                        _locator=self.manager.run_data_loader,
                        _interface_spec=self.plan._handle_spec,
                    ),
                    provenance,
                )

            case core.WorkflowResultHandle(workflow_ref=workflow_ref):
                # Workflow result handles need to iteratively update themselves to follow the
                # workflow path.
                current_ref = workflow_ref
                step_run_cids = []
                while True:
                    if not self.is_current:
                        raise RuntimeError(
                            f"Task handle worker for {_summarize(self.plan)} is stale; not loading"
                            " stale task handles"
                        )
                    node = self.manager._get_node(current_ref)
                    if node is None:
                        raise RuntimeError(f"Workflow step {current_ref} is not in the active set")

                    # Track this node with this worker so that we are notified when the node is dropped.
                    node.workers[id(self)] = self
                    self.nodes.append(node)

                    # Check if the node has a workflow commitment; if so, wait for it.
                    commitment = self.manager.workflow_commitments.get(current_ref)
                    if commitment is not None:
                        await commitment.advanced_or_cancelled_event.wait()
                        if not commitment.succeeded:
                            raise RuntimeError(
                                f"Workflow commitment for step {current_ref} was not fulfilled"
                                f" (status: {commitment.status_message})"
                            )

                    # Check if the node has a completed run. It should at this point.
                    if not isinstance(node.run_or_reservation, core.Run):
                        raise RuntimeError(
                            f"Workflow step {current_ref} does not have a completed run in the"
                            " active set, and also does not have a workflow commitment; cannot"
                            " attach to the completion of this workflow!"
                        )

                    # Collect the run CID for this step
                    run = node.run_or_reservation
                    step_run_cids.append(core.entity_cid(run))

                    if node.workflow_next is not None:
                        # This is a completed workflow step that has a next step. Update the current
                        # reference to the next step and continue.
                        current_ref = node.workflow_next
                        continue
                    else:
                        # By this point the workflow should have completed.
                        provenance = externals_provider.WorkflowResultProvenance(
                            step_run_cids=tuple(step_run_cids)
                        )
                        return (
                            externals_provider.WorkflowResultRetriever(
                                run.run_data, self.manager.run_data_loader, self.plan._handle_spec
                            ),
                            provenance,
                        )
            case _:
                assert False, f"Unexpected plan type: {type(self.plan)}"


class ActiveRunManager:
    """Manager for active runs.

    The active run manager maintains the state of each task in fxtr in a given session. Each task
    can be associated with either (a) a completed run or (b) a scheduled/active run.

    The active run manager maintains the invariants for fxtr runsets: every run sees a _consistent_
    history of all previous runs, such that if run R is in the runset and depends on previous run
    R', then run R' is also in the runset. This property is extended to allow creating plan workers
    for scheduled runs: if a task T has reserved a slot for a run R, and has a dependency on a
    previous task T' (with a plan provided by this active run manager), then task T' must also have
    a reserved slot for run R'.

    The run manager checks task dependencies both (a) at the time a reservation is created and (b)
    at the time the final run is added. Separately, the run manager allows plans to be resolved to
    task result handles whenever a reservation exists for them. The typical pattern of interaction
    here is:
    1. Previous tasks have their reservations created.
    2. A reservation is created for a new task, at which point we check that dependencies have
        appropriate runs and/or run reservations.
    3. The task actually requests its dependencies, which resolve to plans.
    4. The task completes, and fulfills its reservation with a run. Assuming the reservation is
        still active, the run manager then checks that all of the dependencies have appropriate
        runs, which match the active set.
    If the reservation for a task T is removed between step 3 and 4, any plans that depend on that
    task's handle will become stale immediately. Then, when that task T completes, it will not
    be possible to fulfill the reservation since it is no longer active.

    Active run managers also keep track of workflow commitments. A workflow commitment is a promise
    to (reserve and) run the future steps of a workflow until it is complete. Workflow commitments
    are associated with active runs and reservations, and can be used as a backing for workflow
    result handles.
    """

    def __init__(self, config: fxtrconfig.FxtrConfig, run_data_loader: locator.CachingEntityLoader):
        self.config = config
        # Map from each task or workflow ID to the node for that task or workflow.
        self.nodes: dict[core.CID_Of[core.Task | core.Workflow], RunNode] = {}
        self.workflow_commitments: dict[core.CID_Of[core.Workflow], WorkflowCommitmentNode] = {}
        self.run_data_loader = run_data_loader

        # When True, incremental persistence (runset + cache writes) is suppressed (used during
        # bulk operations like drop_all that rewrite the store wholesale afterwards).
        self._suppress_persistence = False

        # When True, `self.nodes` holds the complete graph (eager / empty), so accessors treat it
        # as authoritative. When False (lazy mode), nodes are loaded on demand from the co-located
        # cache and membership is checked against the on-disk runset. Set by _load_runset.
        self._fully_loaded = True

        # Load the active runset if it exists
        self._load_runset()

    # ----------------------------------------------------------------------------------------------
    # Node-graph accessors (lazy)
    #
    # All access to the run-graph nodes goes through these accessors. When `_fully_loaded` (eager /
    # empty), `self.nodes` is authoritative. When not (lazy mode), nodes are materialized on demand
    # from the co-located cache: `_get_node` builds a *stub* (run + workflow info, empty edge dicts)
    # and `_ensure_edges` wires its 1-hop neighborhood when an operation needs to traverse edges.
    # Membership is checked against the on-disk runset rows. See the durable-cache design notes.
    # ----------------------------------------------------------------------------------------------

    def _contains(self, task_cid: core.CID_Of[core.Task | core.Workflow]) -> bool:
        """Whether a node for the given task/workflow CID is in the active set."""
        if task_cid in self.nodes:
            return True
        if self._fully_loaded:
            return False
        return self._runset_row_exists(task_cid)

    def _get_node(self, task_cid: core.CID_Of[core.Task | core.Workflow]) -> RunNode | None:
        """Return the node for the given CID, or None if it is not in the active set.

        In lazy mode, a non-resident node is materialized as a stub from the cache (see
        `_lazy_load_node`).
        """
        node = self.nodes.get(task_cid)
        if node is not None:
            return node
        if self._fully_loaded:
            return None
        return self._lazy_load_node(task_cid)

    def _set_node(self, node: RunNode) -> None:
        """Insert/replace a node in the active set."""
        self.nodes[node.task_cid] = node

    def _remove_node(self, task_cid: core.CID_Of[core.Task | core.Workflow]) -> None:
        """Remove a node from the active set."""
        del self.nodes[task_cid]

    def _iter_dependents(self, node: RunNode) -> list[RunNode]:
        """Return the dependents of a node (authoritative full set).

        In lazy mode this ensures the node's edges are loaded (from persisted `dependent_cids`)
        before reading them, so the dependent set is complete even if some dependents were never
        otherwise touched this session.
        """
        self._ensure_edges(node)
        return list(node.dependents.values())

    def _resident_node_cids(self) -> list[core.CID_Of[core.Task | core.Workflow]]:
        """CIDs of all currently-resident nodes (used by bulk operations like drop_all)."""
        return list(self.nodes.keys())

    def _lazy_load_node(self, task_cid: core.CID_Of[core.Task | core.Workflow]) -> RunNode | None:
        """Materialize a node as a stub from its cache row (edges empty until `_ensure_edges`).

        Returns None if the task is not in the active runset. Raises if the runset has the task but
        the cache row is missing — the startup count check guarantees the cache covers the runset,
        so this indicates corruption that a re-migration / rebuild should fix.
        """
        with self._open_store(autocommit=True) as db:
            runset_val = db.get(self._runset_key(task_cid))
            if runset_val is None:
                return None
            record = db.get(self._cache_key(task_cid))
        if record is None:
            raise RuntimeError(
                f"Run graph cache is missing a row for {task_cid} which is present in the runset;"
                " the cache is corrupt. Re-run the runset cache migration to rebuild it."
            )
        run = record["run"]
        assert isinstance(run, core.Run)
        workflow_contains = record["workflow_contains"]
        node = RunNode(
            task_cid=task_cid,
            run_or_reservation=run,
            dependencies={},
            dependents={},
            workers={},
            workflow_contains=set(workflow_contains) if workflow_contains is not None else None,
            workflow_next=record["workflow_next"],
            edges_loaded=False,
        )
        self.nodes[task_cid] = node
        return node

    def _ensure_edges(self, node: RunNode) -> None:
        """Wire a lazily-loaded node's 1-hop neighborhood (both edge directions) from its cache row.

        No-op when the node's edges are already loaded, in eager mode, or while persistence is
        suppressed (drop_all clears the store wholesale and operates only on resident state).
        Neighbors are loaded as stubs (their own edges remain unloaded); edges are merged into the
        existing dicts, so in-session edges (e.g. reservation dependents) are preserved.
        """
        if node.edges_loaded or self._fully_loaded or self._suppress_persistence:
            return
        record = self._read_cache_record(node.task_cid)
        if record is None:
            node.edges_loaded = True
            return
        for dep_cid in record["dependency_cids"]:
            dep = self._get_node(dep_cid)
            if dep is not None:
                node.dependencies[id(dep)] = dep
                dep.dependents[id(node)] = node
        for dependent_cid in record["dependent_cids"]:
            dependent = self._get_node(dependent_cid)
            if dependent is not None:
                node.dependents[id(dependent)] = dependent
                dependent.dependencies[id(node)] = node
        node.edges_loaded = True

    def _store_counts(self) -> tuple[int, int]:
        """Return (runset_row_count, cache_row_count) via cheap COUNT queries (no value loads)."""
        path = self._active_store_path()
        if not path.exists():
            return (0, 0)
        prefix = api_lib.RUN_GRAPH_CACHE_KEY_PREFIX
        with self._open_store(autocommit=True) as db:
            table = db.tablename
            conn = db.conn
            assert conn is not None
            total = conn.select_one(f'SELECT COUNT(*) FROM "{table}"')[0]
            # GLOB treats '_' literally (unlike LIKE), so the prefix matches exactly.
            cache = conn.select_one(f"SELECT COUNT(*) FROM \"{table}\" WHERE key GLOB '{prefix}*'")[
                0
            ]
        return (total - cache, cache)

    def _runset_row_exists(self, task_cid: core.CID_Of[core.Task | core.Workflow]) -> bool:
        """Whether the runset has a row for the given task CID (indexed lookup)."""
        path = self._active_store_path()
        if not path.exists():
            return False
        with self._open_store(autocommit=True) as db:
            return self._runset_key(task_cid) in db

    def _read_cache_record(self, task_cid: core.CID_Of[core.Task | core.Workflow]) -> dict | None:
        """Read a single node's cache record by CID (indexed lookup), or None if absent."""
        path = self._active_store_path()
        if not path.exists():
            return None
        with self._open_store(autocommit=True) as db:
            return db.get(self._cache_key(task_cid))

    def _load_runset(self, use_threading: bool = True) -> None:
        """Prepare the active run graph from disk.

        If the co-located cache is present and complete, enter lazy mode: build nothing now; nodes
        are loaded on demand from the cache (sub-linear startup). Otherwise — no cache / incomplete
        (e.g. after an external runset overwrite, an old-format runset, or a legacy pickle that
        could not be migrated) — do a one-time resumable rebuild from the entity store (see
        `_cold_build`), then serve lazily; subsequent restarts find a complete cache and go lazy.
        """
        store_path = self._active_store_path()
        legacy_pickle = fxtrconfig.get_active_state_store(self.config) / "_run_graph_cache.pickle"

        runset: core.RunSet | None = None
        if legacy_pickle.exists():
            # Migrating a legacy pickle needs the full runset to validate against.
            runset = self._try_load_active_runset()
            if runset is None:
                return
            self._migrate_legacy_pickle_cache(runset)
        elif not store_path.exists():
            # No new-format store yet: loading migrates any old json/cid format to active.sqlite
            # (or returns empty). Then fall through to the count check.
            runset = self._try_load_active_runset()
            if runset is None:
                return
            if not runset.runs:
                self._clear_run_graph_cache()
                self._fully_loaded = True
                return

        runset_count, cache_count = self._store_counts()

        if runset_count == 0:
            self._clear_run_graph_cache()
            self._fully_loaded = True
            return

        if cache_count == runset_count:
            # Cache is complete: lazy mode. Build nothing; nodes load on demand.
            self._fully_loaded = False
            logger.info(
                f"Active runset has {runset_count} runs; using lazy cache (load on demand)."
            )
            return

        # Cache is absent/incomplete: one-time resumable rebuild from the entity store.
        logger.info(
            f"Run graph cache incomplete ({cache_count}/{runset_count} rows); rebuilding"
            " (resumable)."
        )
        if runset is None:
            runset = self._try_load_active_runset()
            if runset is None:
                return
        self._cold_build(runset)

    def _try_load_active_runset(self) -> core.RunSet | None:
        """Load the active runset, marking the manager fully-loaded (empty) on failure."""
        try:
            return api_lib.load_active_runset(self.config)
        except Exception as e:
            logger.warning(f"Failed to load active runset: {e}")
            self._fully_loaded = True
            return None

    def _cold_build(self, runset: core.RunSet) -> None:
        """Resumably (re)build the run-graph cache in place, then serve lazily.

        Builds a fresh, byte-compatible store (runset rows + `_rg/` cache rows) in a sibling file
        via `RunGraphCacheBuilder` (staging + dest), then atomically swaps it over the active store
        and flips to lazy mode. The build is parallel, provenance-free, and resumable; a crash
        leaves the active store untouched and the sibling files as a checkpoint to continue from.

        Assumes single-writer access to the active store while this runs (nothing else mutates the
        runset). See notes/2026_06_15_durable_cache_pt2.md.
        """
        active = self._active_store_path()
        staging_path = active.with_name(active.name + ".staging")
        dest_path = active.with_name(active.name + ".rebuild")
        builder = RunGraphCacheBuilder(
            source_runs=runset.runs,
            loader=self.run_data_loader,
            staging_path=staging_path,
            dest_path=dest_path,
            copy_runset=True,
        )
        built, skipped = builder.run()
        os.replace(dest_path, active)
        logger.info(
            f"Run-graph cache rebuilt: {built} nodes"
            f"{f' ({skipped} skipped)' if skipped else ''}. Serving lazily."
        )
        self._fully_loaded = False
        self.nodes = {}

    def _check_completed_run_deps(self, task: core.Task | core.Workflow, run: core.Run) -> tuple[
        list[RunNode],
        list[
            core.ImplProvenanceSet
            | externals_provider.RunProvenance
            | externals_provider.WorkflowResultProvenance
        ],
    ]:
        """Check run dependencies and make sure they are compatible with the active set.

        Returns a list of nodes that are dependencies of the given run, and a list of
        provenances loaded during dependency checking.
        """
        # we construct this for the sake of returning it so provenances don't get garbage collected
        # which allows caching to work across calls when calling this function multiple times in a loop
        # (as is done in load_runset).
        provenance_list: list[
            core.ImplProvenanceSet
            | externals_provider.RunProvenance
            | externals_provider.WorkflowResultProvenance
        ] = []

        provenance = self.run_data_loader(run.provenance)
        assert isinstance(provenance, core.ImplProvenanceSet)
        provenance_list.append(provenance)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"RUN {core.entity_cid(run)} has provenance:")
            for dep, prov in provenance.provenances.items():
                logger.debug(f"  {dep}: {_summarize(prov)}")

        dep_nodes: list[RunNode] = []
        for dep in plan_dep_util.plan_deps(task.function, recursive=True):
            match dep:
                case core.TaskResultHandle(task_ref=task_ref) | core.WorkflowStepResultHandle(
                    workflow_ref=task_ref
                ):
                    prev_node = self._get_node(task_ref)
                    if prev_node is None:
                        raise ValueError(
                            f"Previous task {task_ref} does not have a completed run in the active"
                            " set!"
                        )
                    if not isinstance(prev_node.run_or_reservation, core.Run):
                        raise ValueError(
                            f"Previous task {task_ref} does not have a completed run in the active"
                            " set!"
                        )
                    prev_provenance = provenance.provenances.get(core.entity_cid(dep))
                    logger.debug(f"LOOKING UP {core.entity_cid(dep)} in {provenance.provenances}")
                    if not isinstance(
                        prev_provenance, externals_provider.RunProvenance
                    ) or prev_provenance.run_cid != core.entity_cid(prev_node.run_or_reservation):
                        raise ValueError(
                            f"Provenance record {prev_provenance} does not match the completed run"
                            f" {core.entity_cid(prev_node.run_or_reservation)} in the active set!"
                        )
                    dep_nodes.append(prev_node)
                    provenance_list.append(prev_provenance)
                case core.WorkflowResultHandle(workflow_ref=workflow_ref):
                    workflow_path_cids = []
                    current_ref = workflow_ref
                    while current_ref is not None:
                        node = self._get_node(current_ref)
                        if node is None:
                            raise ValueError(
                                f"Workflow {workflow_ref} not found in the active set!"
                            )
                        if not isinstance(node.run_or_reservation, core.Run):
                            raise ValueError(
                                f"Workflow {workflow_ref} does not have a completed run of step"
                                f" {current_ref} in the active set!"
                            )

                        dep_nodes.append(node)
                        workflow_path_cids.append(core.entity_cid(node.run_or_reservation))
                        current_ref = node.workflow_next

                    wf_provenance = provenance.provenances.get(core.entity_cid(dep))
                    if not isinstance(
                        wf_provenance, externals_provider.WorkflowResultProvenance
                    ) or wf_provenance.step_run_cids != tuple(workflow_path_cids):
                        raise ValueError(
                            f"Provenance record {wf_provenance} does not match the completed"
                            f" workflow path {workflow_path_cids} in the active set!"
                        )
                    provenance_list.append(wf_provenance)
                case _:
                    pass

        return dep_nodes, provenance_list

    def _walk_task_deps_for_reservation(
        self, task: core.Task | core.Workflow
    ) -> list[RunNode | WorkflowCommitmentNode]:
        """Walk the dependencies for the given task or workflow.

        Similar to _check_completed_run_deps, but does not require the run or any of its
        dependencies to actually be complete. Instead, they all just need to be present in the
        active set.
        """
        dep_nodes: list[RunNode | WorkflowCommitmentNode] = []
        for dep in plan_dep_util.plan_deps(task.function, recursive=True):
            match dep:
                case core.TaskResultHandle(task_ref=task_ref) | core.WorkflowStepResultHandle(
                    workflow_ref=task_ref
                ):
                    prev_node = self._get_node(task_ref)
                    if prev_node is None:
                        raise ValueError(
                            f"Previous task {task_ref} does not have a run or reservation in the"
                            " active set!"
                        )
                    dep_nodes.append(prev_node)
                case core.WorkflowResultHandle(workflow_ref=workflow_ref):
                    current_ref = workflow_ref
                    while True:
                        node = self._get_node(current_ref)
                        if node is None:
                            raise ValueError(
                                f"Workflow {workflow_ref} not found in the active set!"
                            )
                        wf_commitment = self.workflow_commitments.get(current_ref)
                        if wf_commitment is not None:
                            dep_nodes.append(wf_commitment)
                        else:
                            match node.run_or_reservation:
                                case core.Run():
                                    # Completed run. Add this node to the dependencies and advance to
                                    # the next workflow step.
                                    dep_nodes.append(node)
                                    if node.workflow_next is None:
                                        break
                                    else:
                                        current_ref = node.workflow_next
                                case RunReservation():
                                    raise ValueError(
                                        f"Only one step of workflow {workflow_ref} is scheduled;"
                                        " cannot depend on its final result!"
                                    )
                                case _:
                                    assert False, (
                                        "Unexpected run or reservation type:"
                                        f" {type(node.run_or_reservation)}"
                                    )
                case _:
                    pass

        return dep_nodes

    def _get_workflow_contains_and_next(
        self, run: core.Run
    ) -> tuple[
        set[core.CID_Of[core.Task | core.Workflow]] | None, core.CID_Of[core.Workflow] | None
    ]:
        return _workflow_contains_and_next(run, self.run_data_loader)

    def _add_completed_run(
        self,
        task: core.Task | core.Workflow,
        run: core.Run,
        *,
        persist: bool = True,
    ) -> list[
        core.ImplProvenanceSet
        | externals_provider.RunProvenance
        | externals_provider.WorkflowResultProvenance
    ]:
        """Add a (compatible) completed run for the given task or workflow.

        All dependencies of the task or workflow must have been already completed and added to the
        active run state, and the task must not already have a completed or pending run.

        This is an internal method. Use add_completed_run for the public API.

        Args:
            task: The task or workflow to add the run for.
            run: The run to add.
            persist: If True, atomically write the runset row and cache rows. Set False during a
                cold load, where the runset on disk is already correct and the cache is repopulated
                in bulk afterwards.
        """
        task_cid = core.entity_cid(task)
        existing = self._get_node(task_cid)
        if existing is not None:
            if isinstance(existing.run_or_reservation, core.Run):
                raise ValueError(f"Task {task_cid} already has a completed run")
            else:
                raise ValueError(f"Task {task_cid} already has a pending reservation")

        dep_nodes, provenance_list = self._check_completed_run_deps(task, run)

        if isinstance(task, core.Workflow):
            workflow_contains, workflow_next = self._get_workflow_contains_and_next(run)
        else:
            workflow_contains, workflow_next = None, None

        new_node = RunNode(
            task_cid=task_cid,
            run_or_reservation=run,
            dependencies={id(dep_node): dep_node for dep_node in dep_nodes},
            dependents={},
            workers={},
            workflow_contains=workflow_contains,
            workflow_next=workflow_next,
        )
        for dep_node in dep_nodes:
            dep_node.dependents[id(new_node)] = new_node
        self._set_node(new_node)

        # Atomically insert the runset row, write the new node's own cache row, and surgically add
        # the new node to each dependency's reverse index (dependent_cids).
        if persist:
            self._commit_changes(
                runset_inserts=[(task_cid, core.entity_cid(run))],
                cache_puts=[new_node],
                dependent_additions=[(dep.task_cid, task_cid) for dep in dep_nodes],
            )

        return provenance_list

    def add_completed_run(self, task: core.Task | core.Workflow, run: core.Run) -> None:
        """Add a (compatible) completed run for the given task or workflow.

        All dependencies of the task or workflow must have been already completed and added to the
        active run state, and the task must not already have a completed or pending run.

        Args:
            task: The task or workflow to add the run for.
            run: The run to add.
        """
        self._add_completed_run(task, run)

    def create_reservation(self, task: core.Task | core.Workflow) -> RunReservation:
        task_cid = core.entity_cid(task)
        if self._contains(task_cid):
            raise ValueError(f"Task {task_cid} already has a completed run or reservation")
        dep_nodes = self._walk_task_deps_for_reservation(task)
        reservation = RunReservation(
            task=task, fulfilled_or_cancelled_event=anyio.Event(), status_message=""
        )
        new_node = RunNode(
            task_cid=task_cid,
            run_or_reservation=reservation,
            dependencies={id(dep_node): dep_node for dep_node in dep_nodes},
            dependents={},
            workers={},
            workflow_contains=None,
            workflow_next=None,
        )
        for dep_node in dep_nodes:
            dep_node.dependents[id(new_node)] = new_node
        self._set_node(new_node)
        return reservation

    def fulfill_reservation(self, reservation: RunReservation, run: core.Run) -> None:
        """Fulfill the given task reservation with the given run.

        Reservations can only be fulfilled when all of their dependencies have already been
        fulfilled, and their provenance records must match the runs in the active set.

        Args:
            reservation: The reservation to fulfill.
            run: The run to fulfill the reservation with.
        """
        task_cid = core.entity_cid(reservation.task)
        node = self._get_node(task_cid)
        if node is None:
            raise ValueError(f"Task {task_cid} not found in the active set!")
        if node.run_or_reservation is not reservation:
            raise ValueError(f"Reservation for task {task_cid} is not active!")

        assert not reservation.fulfilled_or_cancelled_event.is_set()

        dep_nodes, _ = self._check_completed_run_deps(reservation.task, run)
        if set(id(dep_node) for dep_node in dep_nodes) != set(node.dependencies.keys()):
            raise RuntimeError(
                f"Internal error: incompatible dependencies for task {task_cid}.\nActive set has"
                f" {dep_nodes}, but reservation dependencies are {node.dependencies.values()}"
            )

        if isinstance(reservation.task, core.Workflow):
            workflow_contains, workflow_next = self._get_workflow_contains_and_next(run)
        else:
            workflow_contains, workflow_next = None, None

        node.run_or_reservation = run
        node.workflow_contains = workflow_contains
        node.workflow_next = workflow_next
        reservation.status_message = f"Fulfilled by {core.entity_cid(run)}"
        reservation.fulfilled_or_cancelled_event.set()

        # The reservation node is now a completed run. Atomically insert its runset row, write its
        # own cache row, and surgically add it to each dependency's reverse index.
        self._commit_changes(
            runset_inserts=[(task_cid, core.entity_cid(run))],
            cache_puts=[node],
            dependent_additions=[(dep.task_cid, task_cid) for dep in dep_nodes],
        )

    def has_run_or_reservation(self, task: core.Task | core.Workflow) -> bool:
        """Check if the given task or workflow has a run or reservation."""
        task_cid = core.entity_cid(task)
        return self._contains(task_cid)

    def reservation_is_active(self, reservation: RunReservation) -> bool:
        """Check if the given task reservation is active."""
        task_cid = core.entity_cid(reservation.task)
        node = self._get_node(task_cid)
        if node is None:
            return False
        return node.run_or_reservation is reservation

    def cancel_reservation(
        self, reservation: RunReservation, status_message: str = "Cancelled"
    ) -> None:
        """Cancel the given task reservation."""
        task_cid = core.entity_cid(reservation.task)
        node = self._get_node(task_cid)
        if node is None:
            raise ValueError(f"Task {task_cid} not found in the active set!")
        if not isinstance(node.run_or_reservation, RunReservation):
            raise ValueError(f"Reservation for task {task_cid} is not active!")
        self.drop_run_for_task(
            task_cid,
            and_dependents=True,
            drop_message=f"Dropped (due to cancellation of {task_cid})",
            drop_workflow_steps=None,  # Should not have contents if we have a reservation
        )
        reservation.status_message = status_message
        reservation.fulfilled_or_cancelled_event.set()

    def create_workflow_commitment(self, workflow: core.Workflow) -> WorkflowCommitmentNode:
        """Create a workflow commitment for the given workflow."""
        task_cid = core.entity_cid(workflow)
        if task_cid in self.workflow_commitments:
            raise ValueError(f"Workflow {task_cid} already has a commitment!")
        if not self._contains(task_cid):
            raise ValueError(
                f"Workflow {task_cid} not found in the active set! The current step of the workflow"
                " must already have a run reservation or a completed run in order to create a"
                " commitment for it."
            )
        commitment = WorkflowCommitmentNode(
            workflow_cid=task_cid,
            advanced_or_cancelled_event=anyio.Event(),
            dependents={},
            status_message="",
        )
        self.workflow_commitments[task_cid] = commitment
        return commitment

    def has_workflow_commitment(self, workflow: core.Workflow) -> bool:
        """Check if the given workflow has a commitment."""
        task_cid = core.entity_cid(workflow)
        return task_cid in self.workflow_commitments

    def workflow_commitment_is_active(self, commitment: WorkflowCommitmentNode) -> bool:
        """Check if the given workflow commitment is active."""
        task_cid = commitment.workflow_cid
        return (
            task_cid in self.workflow_commitments
            and self.workflow_commitments[task_cid] is commitment
        )

    def fulfill_workflow_commitment(self, commitment: WorkflowCommitmentNode) -> None:
        """Mark a workflow commitment as fulfilled.

        A workflow commitment can be marked as fulfilled when (a) the workflow has a completed run,
        and either (b) the run resolves to a final result, or (c) the run resolves to a new workflow
        step AND that workflow step now has its own commitment. (Generally, this new commitment is
        provided by whatever worker made the current commitment.)
        """
        task_cid = commitment.workflow_cid
        if self.workflow_commitments.get(task_cid) is not commitment:
            raise ValueError(f"Workflow commitment for task {task_cid} is not active!")
        assert not commitment.advanced_or_cancelled_event.is_set()

        step_node = self._get_node(task_cid)
        if step_node is None:
            raise ValueError(f"Workflow {task_cid} not found in the active set!")
        run = step_node.run_or_reservation
        if not isinstance(run, core.Run):
            raise ValueError(f"Workflow {task_cid} does not have a completed run!")
        if step_node.workflow_next is None:
            # This workflow is done; no need for a new commitment.
            commitment.status_message = f"Finished by {core.entity_cid(run)}"
        else:
            # This workflow resolved to a new workflow step. Make sure that step has a new commitment.
            next_commitment = self.workflow_commitments.get(step_node.workflow_next)
            if next_commitment is None:
                raise ValueError(
                    f"Next workflow step {step_node.workflow_next} does not have a commitment!"
                    " Workflow step commitments can only be fulfilled once their next step has its"
                    " own commitment."
                )
            commitment.status_message = (
                f"Advanced by {core.entity_cid(run)} to {step_node.workflow_next}"
            )
            # Rebind dependents to this new step and next commitment, since this commitment has now
            # resolved to a new step.
            for dependent in commitment.dependents.values():
                del dependent.dependencies[id(commitment)]
                dependent.dependencies[id(step_node)] = step_node
                dependent.dependencies[id(next_commitment)] = next_commitment

            # The rebind changed the dependency edges of each dependent (they now depend on the
            # completed step node). Rewrite each completed-run dependent's own row (its
            # dependency_cids now includes the step node) and surgically add it to the step node's
            # reverse index. (The next commitment is a transient WorkflowCommitmentNode, never
            # cached; no runset change here.)
            completed_dependents = [
                d
                for d in commitment.dependents.values()
                if isinstance(d.run_or_reservation, core.Run)
            ]
            self._commit_changes(
                cache_puts=completed_dependents,
                dependent_additions=[
                    (step_node.task_cid, d.task_cid) for d in completed_dependents
                ],
            )

        # OK to resolve the commitment.
        del self.workflow_commitments[task_cid]
        commitment.succeeded = True
        commitment.advanced_or_cancelled_event.set()

    def cancel_workflow_commitment(
        self, commitment: WorkflowCommitmentNode, status_message: str = "Cancelled"
    ) -> None:
        """Cancel the given workflow commitment."""
        task_cid = commitment.workflow_cid
        if self.workflow_commitments.get(task_cid) is not commitment:
            raise ValueError(f"Workflow commitment for task {task_cid} is not active!")
        assert not commitment.advanced_or_cancelled_event.is_set()
        commitment.status_message = status_message
        commitment.succeeded = False
        del self.workflow_commitments[task_cid]
        commitment.advanced_or_cancelled_event.set()

    def _drop_run_for_task(
        self,
        task_cid: core.CID_Of[core.Task | core.Workflow],
        and_dependents: bool = False,
        drop_message: str = "Dropped",
        drop_workflow_steps: bool | None = None,
    ) -> None:
        """Drop the (complete or pending) run for the given task from the active run set.

        Args:
            task_cid: The CID of the task to drop.
            and_dependents: If True, also drop downstream dependents.
            drop_message: The message to set on the run or reservation.
            drop_workflow_steps: If True, also drop all steps of a workflow. If None, only drop
               the top-level workflow step. Note that workflow steps can sometimes duplicate tasks
               that were defined outside the workflow, in which case they will also be dropped.
        """
        node = self._get_node(task_cid)
        if node is None:
            raise ValueError(f"Task {task_cid} not found in the active set!")

        # Ensure this node's in-memory edges are fully populated before we traverse/clean them up
        # (a no-op in eager mode; loads the 1-hop neighborhood for a lazily-loaded node).
        self._ensure_edges(node)

        # Maybe drop workflow contents
        if node.workflow_contains is not None:
            if drop_workflow_steps is None:
                raise ValueError(
                    f"Task {task_cid} has workflow contents, but drop_workflow_steps is None. "
                    "Must specify drop_workflow_steps=True or False when dropping a workflow."
                )
            if drop_workflow_steps:
                # Drop all nodes in workflow_contains recursively
                # Use and_dependents=False since we're already handling the workflow's own
                # dependents above, and we don't want to drop things outside the workflow
                # that depend on workflow contents.
                for contained_cid in list(node.workflow_contains):
                    if self._contains(contained_cid):
                        # Only drop if it's still in the active set (might have been dropped already)
                        self._drop_run_for_task(
                            contained_cid,
                            and_dependents=and_dependents,
                            drop_message=drop_message,
                            drop_workflow_steps=True,  # Recursively drop nested workflow steps
                        )

        # Maybe drop dependents
        if and_dependents:
            for dependent in self._iter_dependents(node):
                if not self._contains(dependent.task_cid):
                    # probably removed during this iteration
                    continue
                assert self._get_node(dependent.task_cid) is dependent
                self._drop_run_for_task(
                    dependent.task_cid,
                    and_dependents=True,
                    drop_message=drop_message,
                    drop_workflow_steps=drop_workflow_steps,
                )
            assert not node.dependents
        elif node.dependents:
            raise ValueError(f"Task {task_cid} has downstream dependents in the active set!")

        # Cancel workflow commitment if necessary
        wf_commitment = self.workflow_commitments.get(task_cid)
        if wf_commitment is not None:
            if and_dependents:
                for dependent in list(wf_commitment.dependents.values()):
                    if not self._contains(dependent.task_cid):
                        # probably removed during this iteration
                        continue
                    assert self._get_node(dependent.task_cid) is dependent
                    self._drop_run_for_task(
                        dependent.task_cid,
                        and_dependents=True,
                        drop_message=drop_message,
                        drop_workflow_steps=drop_workflow_steps,
                    )
                assert not wf_commitment.dependents
            else:
                raise ValueError(
                    f"Workflow commitment for task {task_cid} has downstream dependents in the"
                    " active set!"
                )
            wf_commitment.status_message = drop_message
            wf_commitment.advanced_or_cancelled_event.set()
            del self.workflow_commitments[task_cid]

        for worker in list(node.workers.values()):
            assert worker.is_current
            worker.mark_stale()

        for dependency in node.dependencies.values():
            del dependency.dependents[id(node)]

        # Check if this was a completed run (not just a reservation) before deleting
        was_completed_run = isinstance(node.run_or_reservation, core.Run)

        self._remove_node(task_cid)

        # Atomically remove this completed run from the runset and the cache, and surgically remove
        # it from each dependency's reverse index. (Reservations are never written to either store,
        # so there is nothing to update for them.)
        if was_completed_run:
            dep_cids = [
                dep.task_cid for dep in node.dependencies.values() if isinstance(dep, RunNode)
            ]
            self._commit_changes(
                runset_deletes=[task_cid],
                cache_deletes=[task_cid],
                dependent_removals=[(dep_cid, task_cid) for dep_cid in dep_cids],
            )

        if isinstance(node.run_or_reservation, RunReservation):
            node.run_or_reservation.status_message = drop_message
            node.run_or_reservation.fulfilled_or_cancelled_event.set()

    def drop_run_for_task(
        self,
        task_cid: core.CID_Of[core.Task | core.Workflow],
        drop_message: str = "Dropped",
        *,
        and_dependents: bool = True,
        drop_workflow_steps: bool | None = None,
    ) -> None:
        """Drop the (complete or pending) run for the given task from the active run set.

        Args:
            task_cid: The CID of the task to drop.
            drop_message: The message to set on the run or reservation.
            and_dependents: If True, also drop downstream dependents.
            drop_workflow_steps: If True, also drop all steps of a workflow. If None, only drop
               the top-level workflow step. Note that workflow steps can sometimes duplicate tasks
               that were defined outside the workflow, in which case they will also be dropped.
        """
        self._drop_run_for_task(
            task_cid,
            and_dependents=and_dependents,
            drop_message=drop_message,
            drop_workflow_steps=drop_workflow_steps,
        )

    def drop_all(self):
        """Drop all runs from the active run set."""
        # note: workflow steps will be dropped automatically because we are dropping everything.
        # Suppress per-node persistence during the bulk drop, then clear the whole store at once
        # (an empty runset overwrite, which also removes all co-located cache rows), rather than
        # issuing one runset+cache delete per node.
        self._suppress_persistence = True
        try:
            for task_cid in self._resident_node_cids():
                if self._contains(task_cid):
                    self._drop_run_for_task(
                        task_cid, and_dependents=True, drop_workflow_steps=False
                    )
        finally:
            self._suppress_persistence = False
        api_lib.save_active_runset(self.config, core.RunSet(runs=frozendict()))

    def drop_from_container(
        self,
        container: core.AnyFrozenIPLDable,
        drop_message: str = "Dropped",
        *,
        and_dependents: bool = True,
        drop_workflow_steps: bool | None = None,
    ):
        """Drop all runs from tasks and workflows in the given container.

        Args:
            container: The container to drop runs from.
            drop_message: The message to set on the runs or reservations.
            and_dependents: If True, also drop downstream dependents.
            drop_workflow_steps: If True, also drop all steps of workflows. If None, only drop
                the top-level workflow steps.
        """
        tasks_and_workflows = _walk_all_tasks_in_object(container)
        # Drop each task/workflow found in the container
        for task_or_workflow in tasks_and_workflows:
            task_cid = core.entity_cid(task_or_workflow)
            if self._contains(task_cid):
                self._drop_run_for_task(
                    task_cid,
                    and_dependents=and_dependents,
                    drop_message=drop_message,
                    drop_workflow_steps=drop_workflow_steps,
                )

    async def make_task_handle_worker(
        self,
        plan: (
            core.TaskResultHandle[Any]
            | core.WorkflowStepResultHandle[Any, Any]
            | core.WorkflowResultHandle[Any]
        ),
    ) -> PlanWorker[Any]:
        """Make a worker for a task handle."""
        return TaskHandleWorker(plan=plan, manager=self)

    # ------------------------------------------------------------------------------------------
    # Durable, incremental run-graph cache (co-located with the runset)
    #
    # The run graph (self.nodes) is expensive to rebuild from the entity store: each run pulls in
    # its task definition, provenance, and (for workflows) run data, plus a plan-dependency walk.
    # To make reloads cheap we maintain a per-node record (everything needed to reconstruct the
    # node WITHOUT any entity-store access) directly INSIDE the runset's SqliteDict (active.sqlite),
    # under keys prefixed with api_lib.RUN_GRAPH_CACHE_KEY_PREFIX. This co-location lets every
    # mutation write the runset row AND the affected cache rows in a SINGLE SqliteDict transaction
    # (`_commit_changes`), so a crash can never separate a node's membership from its dependency
    # edges. The runset rows themselves are byte-identical to before; runset readers skip the
    # prefixed cache rows (see api_lib._load_runset_from_sqlitedict).
    #
    # Only completed-run nodes are cached (reservations and workflow commitments are transient).
    # Both edge directions are persisted (dependency_cids and dependent_cids) so the format is
    # ready for a future lazy loader; the eager loader below rebuilds edges from dependency_cids
    # and derives dependents from them.
    #
    # Note: external full-overwrites of the active runset (api_lib.save_active_runset, used by the
    # CLI / backend runner) unlink and rewrite active.sqlite, dropping the cache rows. That is
    # correct: the whole runset changed, so the cache is rebuilt on the next load.
    # ------------------------------------------------------------------------------------------

    # How long a store operation waits on a locked database before erroring (handles SQLITE_BUSY
    # from a concurrent reader/writer by retrying rather than failing immediately).
    _BUSY_TIMEOUT_MS = 30_000

    def _active_store_path(self) -> pathlib.Path:
        return api_lib.get_active_runset_path(self.config)

    def _open_store(self, *, autocommit: bool) -> SqliteDict:
        """Open the active store (runset + co-located cache) with a busy timeout set."""
        path = self._active_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        db = SqliteDict(str(path), autocommit=autocommit)
        conn = getattr(db, "conn", None)
        if conn is not None:
            try:
                conn.execute(f"PRAGMA busy_timeout = {self._BUSY_TIMEOUT_MS}")
            except Exception:
                pass
        return db

    @staticmethod
    def _runset_key(task_cid: core.CID_Of[core.Task | core.Workflow]) -> str:
        return task_cid.encode(base="base32")

    @staticmethod
    def _cache_key(task_cid: core.CID_Of[core.Task | core.Workflow]) -> str:
        return api_lib.RUN_GRAPH_CACHE_KEY_PREFIX + task_cid.encode(base="base32")

    @staticmethod
    def _derive_dependent_cids(node: RunNode) -> list:
        """Dependent CIDs derived from a node's in-memory dependents (completed runs only).

        Correct only when the node is edge-complete (eager / bulk paths). Incremental updates use
        surgical additions/removals instead — see _commit_changes.
        """
        return [
            dep.task_cid
            for dep in node.dependents.values()
            if isinstance(dep.run_or_reservation, core.Run)
        ]

    def _node_to_cache_record(self, node: RunNode, dependent_cids: list) -> dict:
        """Build the picklable per-node cache record. `dependent_cids` is supplied by the caller
        (preserved from the existing row on incremental upsert, or derived in bulk paths)."""
        run = node.run_or_reservation
        assert isinstance(run, core.Run)
        dependency_cids = [
            dep.task_cid
            for dep in node.dependencies.values()
            if isinstance(dep, RunNode) and isinstance(dep.run_or_reservation, core.Run)
        ]
        return {
            "run": run,
            "dependency_cids": dependency_cids,
            "dependent_cids": list(dependent_cids),
            "workflow_contains": (
                list(node.workflow_contains) if node.workflow_contains is not None else None
            ),
            "workflow_next": node.workflow_next,
        }

    def _commit_changes(
        self,
        *,
        runset_inserts: Iterable[
            tuple[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Run]]
        ] = (),
        runset_deletes: Iterable[core.CID_Of[core.Task | core.Workflow]] = (),
        cache_puts: Iterable[RunNode] = (),
        cache_deletes: Iterable[core.CID_Of[core.Task | core.Workflow]] = (),
        dependent_additions: Iterable[
            tuple[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Task | core.Workflow]]
        ] = (),
        dependent_removals: Iterable[
            tuple[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Task | core.Workflow]]
        ] = (),
    ) -> None:
        """Atomically apply runset and cache changes in a single SqliteDict transaction.

        This is the one place runset membership and run-graph cache edges are written together, so
        a crash leaves them consistent. The transaction commits or rolls back as a unit; on failure
        we log and leave the on-disk state untouched (a subsequent load self-heals if needed).

        - `cache_puts`: upsert a node's *own* row (run, dependency_cids, workflow info). The row's
          `dependent_cids` (reverse index) is **preserved** from any existing row rather than
          re-derived from the node's possibly-incomplete in-memory `dependents` (a new/fulfilled
          node has none yet). Only completed-run nodes are written.
        - `dependent_additions` / `dependent_removals`: surgically add/remove a `child` from a
          `parent`'s persisted `dependent_cids`. This is how the reverse index is maintained without
          requiring the parent's in-memory dependents to be complete — essential once nodes are
          loaded lazily.
        """
        if self._suppress_persistence:
            return
        runset_inserts = list(runset_inserts)
        runset_deletes = list(runset_deletes)
        put_nodes = [n for n in cache_puts if isinstance(n.run_or_reservation, core.Run)]
        cache_deletes = list(cache_deletes)
        dependent_additions = list(dependent_additions)
        dependent_removals = list(dependent_removals)
        if not (
            runset_inserts
            or runset_deletes
            or put_nodes
            or cache_deletes
            or dependent_additions
            or dependent_removals
        ):
            return
        try:
            with self._open_store(autocommit=False) as db:
                for task_cid in runset_deletes:
                    key = self._runset_key(task_cid)
                    if key in db:
                        del db[key]
                for task_cid, run_cid in runset_inserts:
                    db[self._runset_key(task_cid)] = bytes(run_cid)
                for task_cid in cache_deletes:
                    key = self._cache_key(task_cid)
                    if key in db:
                        del db[key]
                for node in put_nodes:
                    key = self._cache_key(node.task_cid)
                    existing = db.get(key)
                    dependent_cids = (
                        existing["dependent_cids"]
                        if existing is not None
                        else self._derive_dependent_cids(node)
                    )
                    db[key] = self._node_to_cache_record(node, dependent_cids)
                for parent_cid, child_cid in dependent_additions:
                    key = self._cache_key(parent_cid)
                    rec = db.get(key)
                    if rec is not None and child_cid not in rec["dependent_cids"]:
                        rec["dependent_cids"] = [*rec["dependent_cids"], child_cid]
                        db[key] = rec
                for parent_cid, child_cid in dependent_removals:
                    key = self._cache_key(parent_cid)
                    rec = db.get(key)
                    if rec is not None and child_cid in rec["dependent_cids"]:
                        rec["dependent_cids"] = [c for c in rec["dependent_cids"] if c != child_cid]
                        db[key] = rec
                db.commit()
        except Exception as e:
            logger.warning(f"Failed to commit runset/cache changes: {e}")

    def _clear_run_graph_cache(self) -> None:
        """Remove all co-located cache rows (the `_rg/`-prefixed keys), leaving runset rows intact."""
        path = self._active_store_path()
        if not path.exists():
            return
        try:
            with SqliteDict(str(path), autocommit=False) as db:
                stale = [k for k in db.keys() if k.startswith(api_lib.RUN_GRAPH_CACHE_KEY_PREFIX)]
                for key in stale:
                    del db[key]
                db.commit()
        except Exception as e:
            logger.warning(f"Failed to clear run graph cache: {e}")

    def _migrate_legacy_pickle_cache(self, runset: core.RunSet) -> None:
        """Migrate a legacy monolithic pickle cache into co-located rows, then remove it.

        The previous implementation stored the whole node graph as a single
        ``<state_store>/_run_graph_cache.pickle`` (``{"version": 1, "nodes": {task_cid: RunNode}}``).
        If such a file is present and consistent with the runset, we derive per-node records from it
        and write them as ``_rg/`` rows, so an upgrading deployment skips a (potentially very
        expensive) cold rebuild. We only read task CIDs from the pickled nodes, so the old cache's
        shallow-copy edge bug does not matter. The pickle is always deleted afterward; anything
        stale/inconsistent simply falls through to the normal cold rebuild.
        """
        legacy = fxtrconfig.get_active_state_store(self.config) / "_run_graph_cache.pickle"
        if not legacy.exists():
            return
        try:
            with open(legacy, "rb") as f:
                data = pickle.load(f)
            nodes = data["nodes"] if isinstance(data, dict) and data.get("version") == 1 else None
            valid = (
                isinstance(nodes, dict)
                and set(nodes.keys()) == set(runset.runs.keys())
                and all(
                    isinstance(node.run_or_reservation, core.Run)
                    and core.entity_cid(node.run_or_reservation) == runset.runs[task_cid]
                    for task_cid, node in nodes.items()
                )
            )
            if valid:
                assert isinstance(nodes, dict)
                with self._open_store(autocommit=False) as db:
                    stale = [
                        k for k in db.keys() if k.startswith(api_lib.RUN_GRAPH_CACHE_KEY_PREFIX)
                    ]
                    for key in stale:
                        del db[key]
                    for task_cid, node in tqdm(
                        nodes.items(),
                        desc="Migrating legacy pickle cache",
                        total=len(nodes),
                        mininterval=0.1,
                        smoothing=0,
                    ):
                        db[self._cache_key(task_cid)] = self._node_to_cache_record(
                            node, self._derive_dependent_cids(node)
                        )
                    db.commit()
                logger.info(
                    f"Migrated {len(nodes)} nodes from legacy pickle cache into active.sqlite"
                )
            else:
                logger.info("Legacy pickle cache is stale/inconsistent; discarding (will rebuild)")
        except Exception as e:
            logger.warning(f"Failed to migrate legacy pickle cache: {e}; discarding")
        finally:
            try:
                legacy.unlink()
            except Exception:
                pass


# endregion
####################################################################################################
# region: Environment management
####################################################################################################


class CoordinatorBindingWorker(PlanWorker[Any]):
    """Plan worker for runtime coordinator bindings.

    This worker wraps a bound plan and provides its implementation, while building
    provenance that includes all transitive dependencies of the bound plan.
    """

    def __init__(
        self,
        plan: core.ExternalServiceIdentifier[Any],
        binding: core.RuntimeCoordinatorBinding,
        bound_plan_worker: PlanWorker[Any],
    ):
        """Initialize a coordinator binding worker.

        Args:
            plan: The coordinator binding plan (ExternalServiceIdentifier).
            binding: The runtime coordinator binding.
            bound_plan_worker: The worker for the bound plan.
        """
        super().__init__(plan=plan, dependency_workers={bound_plan_worker.plan: bound_plan_worker})
        self.binding = binding
        self.bound_plan_worker = bound_plan_worker

    async def _build_impl(self, dep_impls: dict[core.Plan[Any], Any]) -> tuple[Any, core.AnyEntity]:
        """Build the implementation and provenance for this coordinator binding worker."""
        # Get the bound plan's implementation from dependencies
        bound_impl = dep_impls[self.binding.plan]

        # Get transitive provenance from the bound plan worker
        # This includes the bound plan itself and all its transitive dependencies
        transitive_provenance = self.bound_plan_worker.get_transitive_provenance()
        provenance = externals_provider.RuntimeCoordinatorProvenance(
            bound_plan=self.binding.plan,
            bound_provenances=core.ImplProvenanceSet(
                provenances=frozendict(
                    {core.entity_cid(k): v for k, v in transitive_provenance.items()}
                )
            ),
        )
        return bound_impl, provenance


class RuntimeEnvironmentManager:
    """Manager for runtime environment configuration."""

    def __init__(
        self,
        plan_manager: PlanManager,
        config: fxtrconfig.FxtrConfig,
    ):
        self._plan_manager = plan_manager
        self._config = config

        # Live (non-stale) workers tracked by ID.
        self._live_workers: weakref.WeakValueDictionary[core.Plan[Any], PlanWorker[Any]] = (
            weakref.WeakValueDictionary()
        )

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

        # Providers (always exist, updated when bindings are set)
        self._secrets_provider = externals_provider.SecretsProvider(
            config=self._config,
            declarations={},
        )
        self._runtime_env_provider = externals_provider.RuntimeEnvConfigProvider(
            assignments={},
        )

    def set_runtime_bindings(
        self,
        secret_declarations: dict[
            core.ExternalServiceIdentifier[core.SecretRetriever[Any]],
            core.SecretFromEnvVarDeclaration | core.SecretFromOptionalEnvVarDeclaration,
        ],
        runtime_env_assignments: dict[
            core.ExternalServiceIdentifier[core.RuntimeEnvRetriever[Any]],
            core.RuntimeEnvConfigAssignment,
        ],
        runtime_coordinator_bindings: dict[
            core.ExternalServiceIdentifier[Any],
            core.RuntimeCoordinatorBinding,
        ],
    ) -> None:
        """Set the runtime bindings for the session."""
        # Find plans that changed or were removed
        changed_plans = set()

        # Check for changed/removed secret declarations
        for plan in set(self._active_secret_declarations.keys()):
            old_decl = self._active_secret_declarations.get(plan)
            new_decl = secret_declarations.get(plan)
            if old_decl != new_decl:
                changed_plans.add(plan)

        # Check for changed/removed runtime env assignments
        for plan in set(self._active_runtime_env_assignments.keys()):
            old_assign = self._active_runtime_env_assignments.get(plan)
            new_assign = runtime_env_assignments.get(plan)
            if old_assign != new_assign:
                changed_plans.add(plan)

        # Check for changed/removed runtime coordinator bindings
        for plan in set(self._active_runtime_coordinator_bindings.keys()):
            old_binding = self._active_runtime_coordinator_bindings.get(plan)
            new_binding = runtime_coordinator_bindings.get(plan)
            if old_binding != new_binding:
                changed_plans.add(plan)

        # Invalidate workers for changed plans
        for plan in changed_plans:
            if plan in self._live_workers:
                worker = self._live_workers.get(plan)
                if worker is not None:
                    logger.debug(f"Marking worker {worker} for plan {_summarize(plan)} as stale")
                    worker.mark_stale()
                # Remove from cache
                try:
                    del self._live_workers[plan]
                except KeyError:
                    pass

        # Update caches
        self._active_secret_declarations = secret_declarations
        self._active_runtime_env_assignments = runtime_env_assignments
        self._active_runtime_coordinator_bindings = runtime_coordinator_bindings

        # Update providers
        self._secrets_provider.update_declarations(secret_declarations)
        self._runtime_env_provider.update_assignments(runtime_env_assignments)

    async def make_runtime_env_worker(self, plan: core.Plan[Any]) -> PlanWorker[Any]:
        """Make a worker for a runtime environment configuration."""
        if not isinstance(plan, core.ExternalServiceIdentifier):
            raise ValueError(
                "RuntimeEnvironmentManager only handles ExternalServiceIdentifier plans, got"
                f" {type(plan)}"
            )

        # Check if we already have a live worker for this plan
        if plan in self._live_workers:
            worker = self._live_workers.get(plan)
            if worker is not None and worker.is_current:
                return worker

        # Determine the provider and create worker based on plan type
        if plan in self._active_secret_declarations:
            provider = self._secrets_provider
            worker = ProviderBasedPlanWorker(
                plan=plan,
                dependency_workers={},
                provider=provider,
            )
        elif plan in self._active_runtime_env_assignments:
            provider = self._runtime_env_provider
            worker = ProviderBasedPlanWorker(
                plan=plan,
                dependency_workers={},
                provider=provider,
            )
        elif plan in self._active_runtime_coordinator_bindings:
            # For coordinator bindings, create a CoordinatorBindingWorker
            binding = self._active_runtime_coordinator_bindings[plan]
            bound_plan_worker = await self._plan_manager.request_plan(binding.plan)

            worker = CoordinatorBindingWorker(
                plan=plan,
                binding=binding,
                bound_plan_worker=bound_plan_worker,
            )

            # Decrement bound plan worker user count since CoordinatorBindingWorker
            # increments it in its __init__
            bound_plan_worker.decrement_user_count()
            logger.debug(
                "Created coordinator binding worker %s for %s with binding %s",
                worker,
                plan,
                binding,
            )
        else:
            raise RuntimeError(
                f"Plan {plan.service_id} (namespace: {plan.namespace_id}) does not have a "
                "declaration, assignment, or binding. Ensure the collection provides "
                "a SecretFromEnvVarDeclaration, RuntimeEnvConfigAssignment, or "
                "RuntimeCoordinatorBinding for this plan."
            )

        # Store in live workers cache
        self._live_workers[plan] = worker

        return worker


# endregion
####################################################################################################
# region: Materialization
####################################################################################################


@dataclasses.dataclass
class MaterializationTracker:
    """Tracker for a single materialization request.

    Attributes:
        message: A message describing the materialization request.
        author: The name of the author of the materialization request.
        author_email: The email of the author of the materialization request.
        request_timestamp: The timestamp of the materialization request. Must be a string in RFC 3339 format.
        root: The root of the materialization tree.
        should_continue_after_failures: Whether to continue materialization after tasks fail or are
            cancelled.
        cached: A mapping from task or workflow CIDs to their cached run CIDs.
        completed: A mapping from task or workflow CIDs executed as part of this materialization
            request to their run CIDs.
        failed: A mapping from failed task or workflow CIDs to their error messages.
        failed_tracebacks: A mapping from failed task or workflow CIDs to their full formatted
            tracebacks. Used to write detailed failure dumps; not stored on the immutable record.
        cancelled: A mapping from cancelled task or workflow CIDs to their cancellation messages.
        scheduled_tasks: A mapping from scheduled task or workflow step CIDs to their messages.
        scheduled_commitments: A mapping from workflow CIDs to their messages for full-workflow
            commitments.
        done: Set when all pending tasks and commitments have completed.
    """

    message: str | None
    author: str
    author_email: str
    request_timestamp: str
    root: core.AnyFrozenIPLDable
    should_continue_after_failures: bool
    cached: dict[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Run]]
    completed: dict[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Run]]
    failed: dict[core.CID_Of[core.Task | core.Workflow], str]
    failed_tracebacks: dict[core.CID_Of[core.Task | core.Workflow], str]
    cancelled: dict[core.CID_Of[core.Task | core.Workflow], str]
    scheduled_tasks: dict[core.CID_Of[core.Task | core.Workflow], str]
    scheduled_commitments: dict[core.CID_Of[core.Workflow], str]
    done: anyio.Event


class MaterializationRecord(
    core.DataEntityBase, type_id="MaterializationRecord^1", namespace_id="!builtin"
):
    """Immutable record of a single materialization request."""

    message: str | None
    author: str
    author_email: str
    request_timestamp: str
    root: core.AnyFrozenIPLDable
    should_continue_after_failures: bool
    cached: frozendict[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Run]]
    completed: frozendict[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Run]]
    failed: frozendict[core.CID_Of[core.Task | core.Workflow], str]
    cancelled: frozendict[core.CID_Of[core.Task | core.Workflow], str]


def _fs_safe(name: str, max_len: int = 80) -> str:
    """Sanitize a string into a filesystem-safe fragment for use in a filename."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
    return safe[:max_len] or "unknown"


def _summarize_root(root: core.AnyFrozenIPLDable) -> str:
    """Best-effort one-line summary of a materialization root (any frozen IPLD value)."""
    if isinstance(root, core.EntityBase):
        return _summarize(root)
    if isinstance(root, (tuple, frozendict)):
        return f"{type(root).__name__}(len={len(root)})"
    return f"{type(root).__name__}({root!r})"[:100]


def _save_materialization_record(
    config: fxtrconfig.FxtrConfig,
    record: MaterializationRecord,
    failed_tracebacks: Mapping[core.CID_Of[core.Task | core.Workflow], str] | None = None,
) -> None:
    """Save a materialization record to the fxtr store and state directory.

    Behavior depends on ``config.data.materialization_log_mode``:
    - "per_file" (default): save the entity and write one ``{timestamp}.json`` pointer file.
    - "jsonl": skip cache-only requests (those that scheduled no new work) entirely; otherwise
      save the entity, append a self-describing summary line to ``materializations.jsonl``, and —
      if any task failed — dump the full tracebacks to a per-request file.

    Args:
        config: The fxtr config
        record: The MaterializationRecord to save
        failed_tracebacks: Full formatted tracebacks for failed tasks (jsonl failure dumps).
    """
    if config.data.materialization_log_mode == "jsonl":
        _save_materialization_record_jsonl(config, record, failed_tracebacks or {})
    else:
        _save_materialization_record_per_file(config, record)


def _save_materialization_record_per_file(
    config: fxtrconfig.FxtrConfig, record: MaterializationRecord
) -> None:
    """Legacy mode: save the entity and write one ``{timestamp}.json`` pointer file."""
    # Save the MaterializationRecord entity to the entity store
    data_locator = api_lib.get_active_data_locator(config)
    data_locator.save_entity(record)

    # Get the CID
    record_cid = core.entity_cid(record)
    cid_str = record_cid.encode(base="base32")

    # Save the CID to the state directory
    state_store = fxtrconfig.get_active_state_store(config)
    materializations_dir = state_store / "materializations"

    # Ensure the directory exists
    materializations_dir.mkdir(parents=True, exist_ok=True)

    # Save to file named by request timestamp
    record_file = materializations_dir / f"{record.request_timestamp}.json"
    record_data = {
        "materialization_record_cid": cid_str,
    }
    with open(record_file, "w") as f:
        json.dump(record_data, f, indent=2)

    logger.info(f"Saved materialization record {record_cid} to {record_file}")


def _save_materialization_record_jsonl(
    config: fxtrconfig.FxtrConfig,
    record: MaterializationRecord,
    failed_tracebacks: Mapping[core.CID_Of[core.Task | core.Workflow], str],
) -> None:
    """JSONL mode: skip cache-only requests, else append a summary line (and dump any failures).

    A request that scheduled no new work (only cache hits) is not logged at all — neither the
    entity nor the jsonl line is written.
    """
    # Skip requests that only hit the cache and scheduled no new work.
    if not (record.completed or record.failed or record.cancelled):
        return

    # Save the MaterializationRecord entity so the logged CID resolves.
    data_locator = api_lib.get_active_data_locator(config)
    data_locator.save_entity(record)
    record_cid = core.entity_cid(record)
    cid_str = record_cid.encode(base="base32")

    state_store = fxtrconfig.get_active_state_store(config)
    state_store.mkdir(parents=True, exist_ok=True)

    # If anything failed, dump the full tracebacks to a per-request file for debugging.
    failure_file: str | None = None
    if record.failed:
        failures_dir = state_store / "materialization_failures"
        failures_dir.mkdir(parents=True, exist_ok=True)
        debug_name = _fs_safe(record.message or _summarize_root(record.root))
        failure_path = failures_dir / f"{_fs_safe(record.request_timestamp)}_{debug_name}.txt"
        lines = [
            f"timestamp: {record.request_timestamp}",
            f"record_cid: {cid_str}",
            f"author: {record.author} <{record.author_email}>",
            f"message: {record.message}",
            f"root: {_summarize_root(record.root)}",
            f"failed tasks: {len(record.failed)}",
            "",
        ]
        for task_cid, msg in record.failed.items():
            lines.append("=" * 80)
            lines.append(f"task: {task_cid.encode(base='base32')}")
            lines.append(f"error: {msg}")
            tb = failed_tracebacks.get(task_cid)
            if tb:
                lines.append("")
                lines.append(tb.rstrip())
            lines.append("")
        failure_path.write_text("\n".join(lines))
        failure_file = failure_path.name

    # Append a self-describing summary line to the shared jsonl log.
    summary = {
        "ts": record.request_timestamp,
        "cid": cid_str,
        "message": record.message,
        "author": record.author,
        "root": _summarize_root(record.root),
        "n_cached": len(record.cached),
        "n_completed": len(record.completed),
        "n_failed": len(record.failed),
        "n_cancelled": len(record.cancelled),
        "failed": {k.encode(base="base32"): v for k, v in record.failed.items()},
        "cancelled": {k.encode(base="base32"): v for k, v in record.cancelled.items()},
        "failure_file": failure_file,
    }
    jsonl_path = state_store / "materializations.jsonl"
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(summary) + "\n")

    logger.info(f"Logged materialization record {record_cid} to {jsonl_path}")


class MaterializationWorkerManager:
    """Manager for materialization workers."""

    def __init__(
        self,
        config: fxtrconfig.FxtrConfig,
        plan_manager: PlanManager,
        active_run_manager: ActiveRunManager,
        task_group: anyio.abc.TaskGroup,
        global_shutdown_event: anyio.Event,
        run_data_locator: locator.WritableLocator,
        run_data_loader: locator.CachingEntityLoader,
    ):
        self._config = config
        self._plan_manager = plan_manager
        self._active_run_manager = active_run_manager
        self._task_group = task_group
        self._global_shutdown_event = global_shutdown_event
        self._task_statuses: dict[core.Task | core.Workflow, tuple[str, str]] = {}
        self._run_data_locator = run_data_locator
        self._run_data_loader = run_data_loader

    async def _worker_for_materializing_task(
        self,
        task: core.Task | core.Workflow,
        reservation: RunReservation,
        tracker: MaterializationTracker,
    ):
        """Async worker that materializes a task (or single workflow step) into a reservation.

        This worker guarantees that, when the anyio task it is running in completes or errors, the
        reservation will be appropriately fulfilled or cancelled. Additionally, it will shut
        itself down at an opportune moment if the reservation becomes stale or the shutdown event
        is set.
        """
        self._task_statuses[task] = ("starting", "")
        task_cid = core.entity_cid(task)
        try:
            assert task_cid in tracker.scheduled_tasks
            if (tracker.failed or tracker.cancelled) and not tracker.should_continue_after_failures:
                self._task_statuses[task] = (
                    "cancelled",
                    (
                        "Other tasks aborted in materialization set, and"
                        " should_continue_after_failures=False"
                    ),
                )
                tracker.cancelled[task_cid] = (
                    "Other tasks aborted in materialization set, and"
                    " should_continue_after_failures=False"
                )
                return
            impl_fn_worker = await self._plan_manager.request_plan(task.function)
            try:
                try:
                    impl = await impl_fn_worker.retrieve()
                except Exception as e:
                    if not self._active_run_manager.reservation_is_active(reservation):
                        self._task_statuses[task] = (
                            "cancelled",
                            "Task failed to set up, but no longer has active runset reservation",
                        )
                        tracker.cancelled[task_cid] = (
                            "Task failed to set up, but no longer has active runset reservation"
                        )
                        return
                    else:
                        raise
                transitive_provenance = impl_fn_worker.get_transitive_provenance()
                provenance_record = core.ImplProvenanceSet(
                    provenances=frozendict(
                        {core.entity_cid(k): v for k, v in transitive_provenance.items()}
                    )
                )

                # Check if reservation is still active, and cancel if not
                if not self._active_run_manager.reservation_is_active(reservation):
                    self._task_statuses[task] = ("cancelled", "Lost active runset reservation")
                    tracker.cancelled[task_cid] = "Lost active runset reservation"
                    return
                if self._global_shutdown_event.is_set():
                    self._active_run_manager.cancel_reservation(
                        reservation, status_message="Cancelled by materialization worker shutdown"
                    )
                    self._task_statuses[task] = ("cancelled", "Cancelled by global shutdown")
                    tracker.cancelled[task_cid] = "Cancelled by global shutdown"
                    return
                if (
                    tracker.failed or tracker.cancelled
                ) and not tracker.should_continue_after_failures:
                    self._task_statuses[task] = (
                        "cancelled",
                        (
                            "Other tasks aborted in materialization set, and"
                            " should_continue_after_failures=False"
                        ),
                    )
                    tracker.cancelled[task_cid] = (
                        "Other tasks aborted in materialization set, and"
                        " should_continue_after_failures=False"
                    )
                    return

                # Run the task
                self._task_statuses[task] = ("running", "Task is running")
                if isinstance(task, core.Task):
                    assert core.implements_interface(
                        impl, core.TaskFunction, allow_untyped_impl=True, error_if_not=True
                    )
                    result = await impl.run_task()
                elif isinstance(impl, core.WorkflowFunction):
                    assert core.implements_interface(
                        impl, core.WorkflowFunction, allow_untyped_impl=True, error_if_not=True
                    )
                    result = await impl.run_step()
                else:
                    raise ValueError(f"Unexpected task type: {type(task)}")

                # Create run object for the result
                run_obj = core.Run(
                    run_data=core.entity_cid(result),
                    task=task_cid,
                    provenance=core.entity_cid(provenance_record),
                    run_info=core.BasicRunInfo(
                        author=tracker.author,
                        author_email=tracker.author_email,
                        timestamp=datetime.datetime.now().isoformat(),
                        message=tracker.message,
                        metadata=core.AdHocMetadata(
                            metadata=frozendict(
                                {
                                    "materialization_time": tracker.request_timestamp,
                                }
                            )
                        ),
                    ),
                )

                # Save the run object
                self._run_data_locator.save_entity(task)
                self._run_data_locator.save_entity(result)
                self._run_data_locator.save_entity(provenance_record)
                self._run_data_locator.save_entity(run_obj)

                api_lib.save_run_state(
                    self._config,
                    task_cid,
                    api_lib.TaskRunState(
                        run_name=tracker.message
                        or f"Materialization at {tracker.request_timestamp}",
                        state="completed",
                        status_message="Task completed",
                        run_cid=core.entity_cid(run_obj),
                    ),
                )

                if self._active_run_manager.reservation_is_active(reservation):
                    assert task_cid in tracker.scheduled_tasks
                    tracker.completed[task_cid] = core.entity_cid(run_obj)
                    self._active_run_manager.fulfill_reservation(reservation, run_obj)
                    self._task_statuses[task] = ("completed", "Task completed")
                else:
                    tracker.completed[task_cid] = core.entity_cid(run_obj)
                    self._task_statuses[task] = (
                        "discarded",
                        (
                            "Task completed, but result was removed from active run set before"
                            " completion"
                        ),
                    )
            finally:
                # Need to clean up impl function worker user count since it was incremented in
                # request_plan
                impl_fn_worker.decrement_user_count()
        except anyio.get_cancelled_exc_class():
            if self._active_run_manager.reservation_is_active(reservation):
                self._active_run_manager.cancel_reservation(
                    reservation, status_message="Interrupted by materialization task group shutdown"
                )
            self._task_statuses[task] = (
                "error",
                "Interrupted by materialization task group shutdown",
            )
            tracker.cancelled[task_cid] = "Interrupted by materialization task group shutdown"
            raise
        except BaseException as e:
            if self._active_run_manager.reservation_is_active(reservation):
                self._active_run_manager.cancel_reservation(
                    reservation, status_message=f"Task error: {e}"
                )
            self._task_statuses[task] = ("error", f"Task error: {e}")
            tracker.failed[task_cid] = f"Task error: {e}"
            tracker.failed_tracebacks[task_cid] = traceback.format_exc()
            logging.exception(f"Error in materialization of task {task_cid}")
            # Don't raise here - the exception has already been handled, and we don't want to
            # bring down the whole materialization process.
        finally:
            tracker.scheduled_tasks.pop(task_cid)
            if not tracker.scheduled_tasks and not tracker.scheduled_commitments:
                tracker.done.set()

    async def _worker_for_waiting_on_cached_reservation(
        self,
        task: core.Task | core.Workflow,
        reservation: RunReservation,
        tracker: MaterializationTracker,
    ):
        """Async worker that waits for an existing reservation to complete.

        This worker waits for a reservation that was already created (by another materialization
        request or other process) to complete, then updates the tracker accordingly. If the
        reservation completes successfully, the task is moved to cached. If it is cancelled or fails,
        it is moved to cancelled.

        Args:
            task: The task or workflow that the reservation is for.
            reservation: The existing reservation to wait for.
            tracker: The tracker for the materialization request.
        """
        task_cid = core.entity_cid(task)
        try:
            # Wait for the reservation to be fulfilled or cancelled
            await reservation.fulfilled_or_cancelled_event.wait()

            # Check the current state of the node
            node = self._active_run_manager._get_node(task_cid)
            if node is not None and isinstance(node.run_or_reservation, core.Run):
                # Reservation was fulfilled - move to cached
                tracker.cached[task_cid] = core.entity_cid(node.run_or_reservation)
                self._task_statuses[task] = ("cached", "Completed run found")
                logger.debug(f"Reservation for {_summarize(task)} completed, adding to cached")
            else:
                # Reservation was cancelled or something else happened
                tracker.cancelled[task_cid] = (
                    reservation.status_message
                    or "Result from previous owner materialization did not complete"
                )
                self._task_statuses[task] = (
                    "cancelled",
                    reservation.status_message
                    or "Result from previous owner materialization did not complete",
                )
        except anyio.get_cancelled_exc_class():
            # Our task was cancelled - just mark as cancelled in tracker
            tracker.cancelled[task_cid] = "Interrupted while waiting for reservation"
            self._task_statuses[task] = ("error", "Interrupted while waiting for reservation")
            raise
        except BaseException as e:
            # Some other error occurred
            tracker.cancelled[task_cid] = f"Error while waiting for reservation: {e}"
            self._task_statuses[task] = ("error", f"Error while waiting for reservation: {e}")
            raise
        finally:
            # Remove from scheduled_tasks and check if we're done
            tracker.scheduled_tasks.pop(task_cid, None)
            if not tracker.scheduled_tasks and not tracker.scheduled_commitments:
                tracker.done.set()

    def _materialize_one_soon(
        self,
        task: core.Task | core.Workflow,
        tracker: MaterializationTracker,
        exists_ok: bool = False,
    ):
        """Materialize a task or workflow into the active run set.

        This function immediately creates a reservation for the given task or workflow, and schedules it to be
        materialized into the active run set asynchronously.
        """
        task_cid = core.entity_cid(task)
        if self._active_run_manager.has_run_or_reservation(task):
            node = self._active_run_manager._get_node(task_cid)
            assert node is not None
            if isinstance(node.run_or_reservation, core.Run):
                # Task already has a completed run - add to cached
                tracker.cached[task_cid] = core.entity_cid(node.run_or_reservation)
                logger.debug(
                    f"Task {_summarize(task)} already has a completed run, adding to cached"
                )
                return
            elif isinstance(node.run_or_reservation, RunReservation):
                # Task has a reservation - wait for it to complete
                reservation = node.run_or_reservation
                tracker.scheduled_tasks[task_cid] = "Waiting for existing reservation"
                logger.debug(
                    f"Task {_summarize(task)} already has a reservation, waiting for it to complete"
                )
                self._task_group.start_soon(
                    self._worker_for_waiting_on_cached_reservation, task, reservation, tracker
                )
                return
            else:
                # Should not happen, but handle gracefully
                if exists_ok:
                    logger.debug(
                        f"Skipping materialization of {_summarize(task)} since it already has a run"
                        " or reservation"
                    )
                    return
                else:
                    raise ValueError(f"Task {task_cid} already has a run or reservation")
        assert (
            core.entity_cid(task) not in tracker.scheduled_tasks
        ), f"Task {core.entity_cid(task)} already has a scheduled task!"
        reservation = self._active_run_manager.create_reservation(task)
        tracker.scheduled_tasks[task_cid] = "Scheduled"
        logger.info(f"Materialization of {_summarize(task)} scheduled")
        self._task_group.start_soon(self._worker_for_materializing_task, task, reservation, tracker)

    def materialize_task_soon(
        self, task: core.Task, tracker: MaterializationTracker, exists_ok: bool = False
    ):
        """Materialize a task into the active run set.

        This function immediately creates a reservation for the given task, and schedules it to be
        materialized into the active run set asynchronously.

        When the task completes, if the reservation is still active, it will be fulfilled with the
        results of the task. If the task has an error, the reservation will be cancelled; similarly
        if the reservation is cancelled, the task results will be discarded.

        Args:
            task: The task to materialize.
            tracker: The tracker for the materialization request.
            exists_ok: If True, do not raise an error if the task already has a run or reservation,
                and instead do nothing in that case (since the task either already has been
                materialized or already has some worker that is responsible for it)
        """
        self._materialize_one_soon(task, tracker, exists_ok=exists_ok)

    def materialize_workflow_step_soon(
        self, workflow_step: core.Workflow, tracker: MaterializationTracker, exists_ok: bool = False
    ):
        """Materialize a workflow step into the active run set.

        This function immediately creates a reservation for the given workflow step, and schedules
        it to be materialized into the active run set asynchronously. It is the equivalent of
        `materialize_task_soon` for a workflow step.

        Args:
            workflow_step: The workflow step that should be materialized.
            tracker: The tracker for the materialization request.
            exists_ok: If True, do not raise an error if the step already has a run or reservation,
                and instead do nothing in that case (since the step either already has been
                materialized or already has some worker that is responsible for it)
        """
        self._materialize_one_soon(workflow_step, tracker, exists_ok=exists_ok)

    async def _worker_for_materializing_full_workflow(
        self,
        workflow: core.Workflow,
        commitment: WorkflowCommitmentNode,
        tracker: MaterializationTracker,
    ):
        """Async worker that materializes a full workflow and manages its commitment.

        This worker guarantees that, when the anyio task it is running in completes or errors, the
        commitment will be appropriately advanced or cancelled. It also manages the lifecycle of the
        additional tasks necessary: it will materialize every task demanded by the workflow before
        advancing the commitment.

        Args:
            workflow: The workflow to materialize.
            commitment: The commitment to manage.
        """
        commitment.status_message = "Scheduling workflow"
        wf_cid = core.entity_cid(workflow)
        try:
            # Attach to the run reservation for the first step if necessary, and wait for it to
            # complete
            node = self._active_run_manager._get_node(wf_cid)
            if node is None:
                raise RuntimeError(f"Workflow step {wf_cid} not found in the active set!")

            if isinstance(node.run_or_reservation, RunReservation):
                await node.run_or_reservation.fulfilled_or_cancelled_event.wait()
                if not isinstance(node.run_or_reservation, core.Run):
                    raise RuntimeError(f"Workflow step {wf_cid} reservation was cancelled!")

            run = node.run_or_reservation
            assert isinstance(run, core.Run)
            run_res = self._run_data_loader(run.run_data)
            match run_res:
                case core.WorkflowResult():
                    # This workflow is done! Resolve immediately.
                    if self._active_run_manager.workflow_commitment_is_active(commitment):
                        self._active_run_manager.fulfill_workflow_commitment(commitment)
                    return
                case core.WorkflowState():
                    # At this point the next workflow step should have a commitment, so we can
                    # advance our own commitment.
                    if self._active_run_manager.workflow_commitment_is_active(commitment):
                        # This workflow is not done yet! Schedule all of the next steps. This will
                        # implicitly also schedule the next workflow step if needed.
                        if (
                            tracker.failed or tracker.cancelled
                        ) and not tracker.should_continue_after_failures:
                            self._active_run_manager.cancel_workflow_commitment(
                                commitment,
                                status_message=(
                                    "Workflow cancelled due to failed or cancelled tasks and"
                                    " should_continue_after_failures=False"
                                ),
                            )
                            return
                        else:
                            self.materialize_all_soon(run_res, tracker)
                            self._active_run_manager.fulfill_workflow_commitment(commitment)
                    return
                case _:
                    assert False, f"Unexpected run result type: {type(run_res)}"
        except anyio.get_cancelled_exc_class():
            if self._active_run_manager.workflow_commitment_is_active(commitment):
                self._active_run_manager.cancel_workflow_commitment(
                    commitment, status_message="Interrupted by materialization task group shutdown"
                )
            raise
        except BaseException as e:
            if self._active_run_manager.workflow_commitment_is_active(commitment):
                logging.exception(f"Error in materialization of workflow {wf_cid}")
                self._active_run_manager.cancel_workflow_commitment(
                    commitment, status_message=f"Workflow error: {e}"
                )
            # Don't raise here - the exception has already been handled, and we don't want to
            # bring down the whole materialization process.
            # Also, if this workflow commitment is already cancelled, we don't need to log an error
            # since failure may be expected at that point.
        finally:
            tracker.scheduled_commitments.pop(wf_cid)
            if not tracker.scheduled_tasks and not tracker.scheduled_commitments:
                tracker.done.set()

    def materialize_full_workflow_soon(
        self, workflow: core.Workflow, tracker: MaterializationTracker
    ):
        """Materialize a full workflow into the active run set.

        This function immediately creates both a run reservation and a workflow commitment for the
        given workflow, and schedules workers to materialize all steps of the workflow. This method
        can be used to schedule an entire workflow for materialization.

        As soon as this function returns, the workflow's final result handle can be requested,
        and will block until the workflow completes (instead of failing due to the workflow not
        being scheduled).

        In the background, all of the tasks demanded by the workflow will be materialized
        iteratively, and added to the active run set as they complete. If an error occurs, the
        downstream tasks will be cancelled, but any workflow steps that completed successfully will
        remain in the active run set.

        Full-workflow materializations will always materialize _every_ task produced by the
        workflow as it executes. Usually, subsequent workflow steps depend on the subtasks they
        produce, but in rare cases a workflow may dynamically produce subtasks without then
        requesting their results in a later step. In this situation, if those subtasks fail, but
        the overall materialization configuration allows partial materialization, this
        will NOT interrupt the overall workflow execution, and the overall workflow may complete
        with a successful result, even if some unused subtasks failed. The failed subtasks will
        still be tracked as part of the materialization record for the overall task execution that
        includes this workflow, and should be consulted for detailed task-execution state.

        Workflow materializations will always silently re-use tasks and workflow steps that
        have already been materialized, including resuming failed workflow executions. This is
        actually important because, under fxtr's execution model, a workflow step may dynamically
        request subtasks that have already been materialized but were not previously known to be
        related to this workflow. This means that `materialize_full_workflow_soon` effectively
        always has the `exists_ok` argument set to True. (Note that, in special cases, if these
        tasks are being concurrently materialized by a previous materialization request, this
        request will adopt those results (and their success/failure status) instead of scheduling
        them itself into the new materialization request.)

        As a consequence of interactions between the "materialize every task" and "silently re-use
        tasks" behaviors, if (a) you have a workflow that dynamically produces subtasks but does
        not request their results in a later step, (b) you previously executed that workflow, and
        it produced a final result, but some of these unused subtasks failed, and then (c) you
        again request a full materialization of the workflow, what will happen is that the failed
        tasks will be immediately retried, but the overall final workflow result will remain
        unchanged, and those results will be immediately available without waiting for those
        subtasks to complete (in fact the workflow result will have already been available for use
        even without making the second materialization request, and those results will continue
        to be available).

        Args:
            workflow: The workflow to materialize.
            tracker: The tracker for the materialization request.
        """
        # First make sure there is a reservation for the first step of the workflow, so that we can
        # attach a workflow commitment.
        self.materialize_workflow_step_soon(workflow, tracker, exists_ok=True)
        # Next, create a workflow commitment for the workflow.
        if not self._active_run_manager.has_workflow_commitment(workflow):
            assert (
                core.entity_cid(workflow) not in tracker.scheduled_commitments
            ), f"Workflow {core.entity_cid(workflow)} already has a commitment!"
            commitment = self._active_run_manager.create_workflow_commitment(workflow)
            tracker.scheduled_commitments[core.entity_cid(workflow)] = "Scheduled"
            self._task_group.start_soon(
                self._worker_for_materializing_full_workflow, workflow, commitment, tracker
            )

    def materialize_all_soon(
        self,
        task_container: core.AnyFrozenIPLDable,
        tracker: MaterializationTracker,
    ):
        """Materialize all tasks and workflows contained in a container.

        This function materializes all tasks and workflows contained in a container, and also adds
        them to the given materialization tracker.

        Tasks and workflows are sorted topologically to ensure that dependencies are materialized
        first. However, if a task in the container has a dependency that is outside of the
        container and not already materialized, this is an error.

        Workflows will be fully materialized if found in the container.

        Args:
            task_container: The container to materialize.
            tracker: The tracker for the materialization request.
        """
        # Extract tasks and workflows
        tasks = _walk_all_tasks_in_object(task_container)

        # Sort them by dependency order
        sorted_tasks = _topologically_sort_tasks(tasks)

        # Schedule each
        for task_or_workflow in sorted_tasks:
            match task_or_workflow:
                case core.Task():
                    self.materialize_task_soon(task_or_workflow, tracker, exists_ok=True)
                case core.Workflow():
                    self.materialize_full_workflow_soon(task_or_workflow, tracker)


# endregion
####################################################################################################
# region: UV python management
####################################################################################################


def _extract_uv_source_from_package(package_name: str) -> dict[str, str | bool]:
    """Extract UV source configuration from a package using metadata."""
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


@contextlib.asynccontextmanager
async def _setup_uv_provider(
    config: fxtrconfig.FxtrConfig,
) -> AsyncGenerator[uv_py_provider.UVManagedPyImplProvider, None]:
    """Set up a UV provider for Python plans."""
    # Set up UV provider for Python plans
    virtual_env_dir = pathlib.Path(config.provider.uv.virtual_env_dir)

    # Get UV configuration from fxtr config
    uv_config_dict: dict[str, object] = dict(config.provider.uv.custom_uv_config)

    # Get existing sources from custom config or create new dict
    existing_sources = uv_config_dict.get("sources", {})
    if isinstance(existing_sources, dict):
        sources: dict[str, object] = dict(existing_sources)
    else:
        sources = {}

    # Add local source packages from config
    config_dir = config.config_path.parent
    for pkg_name, rel_path in config.provider.uv.local_source_packages.items():
        sources[pkg_name] = {
            "path": str((config_dir / rel_path).resolve()),
            "editable": True,
        }

    # Add dynamic source packages from config
    for pkg_name in config.provider.uv.dynamic_source_packages:
        sources[pkg_name] = _extract_uv_source_from_package(pkg_name)

    # Update sources in config (nondestructively)
    if sources:
        uv_config_dict["sources"] = sources

    async with uv_py_provider.running_uv_managed_provider(
        virtual_env_dir=virtual_env_dir,
        uv_config=uv_config_dict if uv_config_dict else None,
        remove_env_on_exit=True,
    ) as provider:
        yield provider


# endregion
####################################################################################################
# region: Root session manager public API
####################################################################################################


class AbstractSessionManager(
    core.FxtrCallTarget,
    interface_id="AbstractSessionManager^1",
    namespace_id="!builtin.session_manager",
    stateless=False,
):
    """Abstract base class for a session manager."""

    @abc.abstractmethod
    async def instantiate[T: core.FxtrCallTarget](self, plan: core.Plan[T]) -> T:
        """Instantiate an implementation of a plan.

        Note that instantiating a plan does not materialize any tasks. If the plan depends on tasks
        that have not yet been executed, this will fail. However, if the plan depends on tasks that
        are currently being materialized, `instantiate` will block until the tasks finish.

        Args:
            plan: The plan to instantiate.

        Returns:
            The instantiated implementation of the plan, exposing its methods to Python.
        """
        ...


# endregion
####################################################################################################
# region: Root session manager implementation
####################################################################################################


class _BackendRunnerPlanForwarder(core.FxtrCallTarget):
    """Forwarder for a plan implementation from the BackendRunner."""

    def __init__(self, impl: core.FxtrCallTarget):
        self._impl = impl

    def interface_spec(self) -> core.InterfaceSpec[core.FxtrCallTarget]:
        return self._impl.interface_spec()

    def __getattr__(self, method: str) -> Any:
        """Proxy method calls to the implementation."""
        return getattr(self._impl, method)


class SessionManager(AbstractSessionManager):
    """Session manager implementation."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        _task_group: anyio.abc.TaskGroup,
        _shutdown_event: anyio.Event,
        _python_provider: (
            uv_py_provider.UVManagedPyImplProvider | in_process_py_provider.InProcessPyProvider
        ),
        _run_data_locator: locator.WritableLocator,
        _run_data_loader: locator.CachingEntityLoader,
        _config: fxtrconfig.FxtrConfig,
    ):
        self._task_group = _task_group
        self._shutdown_event = _shutdown_event
        self._run_data_locator = _run_data_locator
        self._run_data_loader = _run_data_loader
        self._config = _config
        weak_self = weakref.proxy(self)
        self._plan_manager = PlanManager(
            task_group=_task_group,
            shutdown_event=_shutdown_event,
            worker_builder=lambda plan: weak_self._make_worker(plan),
        )
        self._active_run_manager = ActiveRunManager(
            config=_config,
            run_data_loader=_run_data_loader,
        )
        self._python_provider = _python_provider
        self._runtime_env_manager = RuntimeEnvironmentManager(
            plan_manager=self._plan_manager,
            config=_config,
        )
        self._materialization_worker_manager = MaterializationWorkerManager(
            config=_config,
            plan_manager=self._plan_manager,
            active_run_manager=self._active_run_manager,
            task_group=_task_group,
            global_shutdown_event=_shutdown_event,
            run_data_locator=_run_data_locator,
            run_data_loader=_run_data_loader,
        )

    @classmethod
    @contextlib.asynccontextmanager
    async def create(
        cls,
        config: fxtrconfig.FxtrConfig,
        *,
        in_process_python: bool = True,
    ) -> AsyncGenerator[SessionManager, None]:
        """Create a SessionManager with proper lifecycle management."""
        run_data_locator = api_lib.get_active_data_locator(config)
        run_data_loader = locator.CachingEntityLoader.from_locator(run_data_locator)
        shutdown_event = anyio.Event()
        async with contextlib.AsyncExitStack() as stack:
            task_group = await stack.enter_async_context(anyio.create_task_group())
            if in_process_python:
                python_provider = in_process_py_provider.InProcessPyProvider()
            else:
                python_provider = await stack.enter_async_context(_setup_uv_provider(config))

            runner = cls(
                _python_provider=python_provider,
                _task_group=task_group,
                _shutdown_event=shutdown_event,
                _run_data_locator=run_data_locator,
                _run_data_loader=run_data_loader,
                _config=config,
            )
            try:
                yield runner
            finally:
                # The durable run-graph cache is maintained incrementally on every mutation, so
                # there is nothing to flush at shutdown.
                shutdown_event.set()

    def _materialize_soon(
        self,
        task_container: core.AnyFrozenIPLDable,
        message: str | None = None,
        should_continue_after_failures: bool = False,
    ) -> MaterializationTracker:
        """Schedule materialization of a task container and return a tracker.

        This method creates a tracker, schedules all tasks and workflows in the container
        for materialization, and returns the tracker immediately without waiting for
        completion.

        Args:
            task_container: The container to materialize.
            message: Optional message describing the materialization request.
            should_continue_after_failures: Whether to continue materialization after tasks fail.

        Returns:
            The MaterializationTracker for this materialization request.
        """
        tracker = MaterializationTracker(
            message=message,
            author=self._config.author.name,
            author_email=self._config.author.email,
            request_timestamp=datetime.datetime.now().isoformat(),
            root=task_container,
            should_continue_after_failures=should_continue_after_failures,
            cached={},
            completed={},
            failed={},
            failed_tracebacks={},
            cancelled={},
            scheduled_tasks={},
            scheduled_commitments={},
            done=anyio.Event(),
        )
        self._materialization_worker_manager.materialize_all_soon(task_container, tracker)
        # If no tasks were scheduled, mark tracker as done
        if not tracker.scheduled_tasks and not tracker.scheduled_commitments:
            tracker.done.set()
        return tracker

    async def materialize(
        self,
        task_container: core.AnyFrozenIPLDable,
        message: str | None = None,
        should_continue_after_failures: bool = False,
    ) -> MaterializationRecord:
        """Materialize a task container and return a record.

        This method creates a tracker, schedules all tasks and workflows in the container
        for materialization, waits for all materialization to complete, and returns a
        MaterializationRecord containing the final results.

        Args:
            task_container: The container to materialize.
            message: Optional message describing the materialization request.
            should_continue_after_failures: Whether to continue materialization after tasks fail.

        Returns:
            A MaterializationRecord containing the final state of the materialization.
        """
        tracker = self._materialize_soon(
            task_container,
            message=message,
            should_continue_after_failures=should_continue_after_failures,
        )
        await tracker.done.wait()
        record = MaterializationRecord(
            message=tracker.message or None,
            author=tracker.author,
            author_email=tracker.author_email,
            request_timestamp=tracker.request_timestamp,
            root=tracker.root,
            should_continue_after_failures=tracker.should_continue_after_failures,
            cached=frozendict(tracker.cached),
            completed=frozendict(tracker.completed),
            failed=frozendict(tracker.failed),
            cancelled=frozendict(tracker.cancelled),
        )
        _save_materialization_record(self._config, record, tracker.failed_tracebacks)
        return record

    async def drop_all_runs(self) -> None:
        """Drop all runs from the active run set."""
        self._active_run_manager.drop_all()

    async def drop_runs(
        self,
        task_container: core.AnyFrozenIPLDable,
        drop_message: str = "Dropped",
        *,
        and_dependents: bool = True,
        drop_workflow_steps: bool | None = None,
    ) -> None:
        """Drop all runs from tasks and workflows in the given container.

        Args:
            task_container: The container to drop runs from.
            drop_message: The message to set on the runs or reservations.
            and_dependents: If True, also drop downstream dependents.
            drop_workflow_steps: If True, also drop all steps of workflows. If None, only drop
                the top-level workflow steps.
        """
        self._active_run_manager.drop_from_container(
            task_container,
            drop_message,
            and_dependents=and_dependents,
            drop_workflow_steps=drop_workflow_steps,
        )

    async def _make_worker(self, plan: core.Plan[Any]) -> PlanWorker[Any]:
        """Make a new plan worker for the given plan."""
        logger.debug(f"Making worker for {_summarize(plan)}")
        # Determine the provider for this plan
        match plan:
            case py_impl_plan.PyImplPlan():
                provider = self._python_provider

                # Fetch dependency workers
                dep_workers = {}
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

            case core.ExternalServiceIdentifier():
                # Route runtime environment plans to RuntimeEnvironmentManager
                worker = await self._runtime_env_manager.make_runtime_env_worker(plan)
                return worker

            case core.ConstantHandle():
                provider = externals_provider.ConstantProvider()
                worker = ProviderBasedPlanWorker(
                    plan=plan, dependency_workers={}, provider=provider
                )
                return worker

            case core.PinnedResultHandle():
                provider = provider_base.SimpleCallableProvider(self._provide_basic)
                worker = ProviderBasedPlanWorker(
                    plan=plan, dependency_workers={}, provider=provider
                )
                return worker

            case (
                core.TaskResultHandle()
                | core.WorkflowStepResultHandle()
                | core.WorkflowResultHandle()
            ):
                worker = await self._active_run_manager.make_task_handle_worker(plan)
                return worker

            case _:
                raise ValueError(f"Unsupported plan type: {type(plan)}")

    async def _provide_basic(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[Any, core.AnyEntity]:
        match plan:
            case core.PinnedResultHandle(provenance=provenance):
                if provenance_lock is not None and provenance_lock != provenance:
                    raise RuntimeError(
                        "PinnedRunHandleProvider received provenance_lock "
                        f"{type(provenance_lock)}, expected matching provenance or None"
                    )
                match provenance:
                    case externals_provider.RunProvenance(run_cid=run_cid):
                        run = self._run_data_loader(run_cid)
                        assert isinstance(run, core.Run)
                        impl = externals_provider.RunDataRetriever(
                            run.run_data, self._run_data_loader, plan.interface_spec
                        )
                        return impl, provenance
                    case externals_provider.WorkflowResultProvenance(step_run_cids=step_run_cids):
                        last_run = self._run_data_loader(step_run_cids[-1])
                        assert isinstance(last_run, core.Run)
                        impl = externals_provider.WorkflowResultRetriever(
                            last_run.run_data, self._run_data_loader, plan.interface_spec
                        )
                        return impl, provenance
                    case _:
                        raise RuntimeError(f"Unsupported provenance type: {type(provenance)}")

            case _:
                raise ValueError(f"Unsupported plan type: {type(plan)}")

    async def instantiate[T: core.FxtrCallTarget](self, plan: core.Plan[T]) -> T:
        impl, _ = await self.instantiate_with_provenance(plan)
        return impl

    async def instantiate_with_provenance[
        T: core.FxtrCallTarget
    ](self, plan: core.Plan[T]) -> tuple[T, dict[core.Plan[Any], core.AnyEntity]]:
        """Instantiate a plan and return both the implementation and transitive provenance.

        Args:
            plan: The plan to instantiate

        Returns:
            Tuple of (implementation, transitive_provenance) where transitive_provenance
            is a dict mapping dependency plans to their provenance entities.
        """
        worker = await self._plan_manager.request_plan(plan)
        try:
            impl = await worker.retrieve()
            forwarder = _BackendRunnerPlanForwarder(impl)
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


async def spawn_session_manager_in_asyncio_task(
    config: fxtrconfig.FxtrConfig,
    *,
    in_process_python: bool = True,
) -> tuple[SessionManager, asyncio.Task[None]]:
    """Spawn a SessionManager within an asyncio task, returning the session manager and the task.

    This function ensures the SessionManager instance is created and accessible
    by threading it out via a future. If session manager creation fails, the
    exception is propagated.

    This is a convenience method to make it easier to work with SessionManager interactively in
    a Jupyter notebook or other interactive environment, without having to use a top-level
    context manager. It uses asyncio directly rather than anyio because anyio does not natively
    support tasks outside of a task group.
    """

    session_manager_fut: asyncio.Future[SessionManager] = asyncio.get_event_loop().create_future()

    async def _go():
        try:
            async with SessionManager.create(
                config, in_process_python=in_process_python
            ) as session_manager:
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


# endregion
