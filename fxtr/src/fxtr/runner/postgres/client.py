"""The local project client: the project, as user code talks to it, with every job run in the
process that submitted it.

:class:`LocalProjectClient` is the :class:`~fxtr.project.client.ProjectClient` over a
:class:`~fxtr.runner.postgres.database.Database`. It is the project's entity store (a write-behind
:class:`~fxtr.runner.postgres.entities.PostgresEntityStorage` the client delegates to, so entities a
user stores are persisted by the next operation that references them, or by an explicit
``await client.entities.flush()``), the project arrays (:mod:`fxtr.runner.postgres.project_arrays`,
under the ABC's names and records), the jobs, and the step cache.

**Jobs run here.** The client is an async context manager and nothing else: entering it opens a
task group, and every ``submit`` or ``resume`` creates the job attempt and starts a
:class:`~fxtr.runner.postgres.runner.JobRunner` for it in that group, returning a handle at once.
Leaving the block normally waits for every job the client started; leaving it with an exception
cancels them, which abandons their running attempts and settles their job attempts so a resume
continues them. Several jobs may run under one client.

**Ctrl-C.** While entered, the client handles the interrupt signal for all its jobs at once (unless
``handle_sigint`` is off), and prints what each press does. The first press stops every running job
gently: nothing more starts, and work already running finishes and is kept (a step in flight
completes into the cache). The second stops the running work too, abandoning it. The third, or a
press while nothing is running, stops the client: the code inside the block is cancelled and the
block raises ``KeyboardInterrupt``. After the first press the client accepts no new jobs. Every
stopped job can be resumed. ``interrupt()`` is the same thing called from code.

**Where the client comes from.** ``from_project`` is how a launcher script or the CLI gets one: it
locates the project (:class:`~fxtr.project.environment.Environment`), imports the project's
declared modules so their steps and workflows are registered, connects to the project's database
in its schema, and creates the runner's tables on entry if they do not exist. The constructor takes
a ``Database`` and a ``Project`` directly, for tests and for tools that already have both.

**What the local client exposes beyond the ABC.** ``entities``, the underlying store, for
``flush`` and the raw byte reads the viewer wants; ``jobs``, the
:class:`~fxtr.runner.postgres.jobs.Jobs` inspection surface (episodes, per-invocation and per-step
states, descriptions), which the viewer reads and the ABC's ``status`` is derived from; and
``database``.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import signal
import sys
import uuid
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self

import anyio
import anyio.abc
import anyio.to_thread
from anyio import AsyncContextManagerMixin

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID, FxtrDataValue, StorableFxtrDataValue
from fxtr.entity_defns.array import Array, ArrayTypeSpec, KeySpec
from fxtr.experiment.registry import GLOBAL_REGISTRY, FunctionRegistry, RegistryError
from fxtr.experiment.workflows import Workflow
from fxtr.project.client import (
    AdoptReport,
    AdoptSkipReason,
    ArrayInfo,
    CacheEntry,
    CacheEntryState,
    ImportRefusedError,
    JobHandle,
    JobID,
    JobNotFinishedError,
    JobNotFoundError,
    JobRunningError,
    JobStatus,
    JobSummary,
    ProjectClient,
    ProjectClientError,
    Snapshot,
)
from fxtr.project.code_version import CodeVersion, function_sources
from fxtr.project.config import Project, load_env_file
from fxtr.project.environment import Environment, LocalEnvironment, Problem
from fxtr.project.viewer import find_viewer
from fxtr.runner.cache_addresses import parse_pattern
from fxtr.runner.driver import in_process_executor
from fxtr.runner.postgres import execution, job_records, ledger
from fxtr.runner.postgres.database import Database, Transaction
from fxtr.runner.postgres.entities import PostgresEntityStorage
from fxtr.runner.postgres.execution import JobAttemptRecord, job_topic
from fxtr.runner.postgres.jobs import JobReport, Jobs
from fxtr.runner.postgres.ledger import StepResultID
from fxtr.runner.postgres.project_arrays import ProjectArrayInfo, ProjectArrays, SnapshotInfo
from fxtr.runner.postgres.runner import (
    JobEvictedError,
    JobRunner,
    RunLimits,
    RunnerSettings,
    default_owner,
)
from fxtr.runner.postgres.status_queries import StepStateRecord

logger = logging.getLogger(__name__)


class EnvironmentNotSyncedError(ProjectClientError):
    """The environment does not match the project (:meth:`Environment.check_synced` found
    problems), so the launch was refused. The message lists each problem with its fix, and how to
    skip the check."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        lines = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(
            "The environment does not match the project, so the launch was refused:\n"
            f"{lines}\nFix the problems, or pass verify_environment=False to run anyway (the job"
            " attempt then records that its environment was not verified)."
        )
        self.problems = tuple(problems)


@dataclasses.dataclass
class _Run:
    """One job this client is running: its runner's task, and how the task ended.

    Attributes:
        runner: The runner, for interrupts.
        done: Set when the runner's task has ended, however it ended.
        error: What the task raised, if it raised: an eviction (a hard cancel or a takeover), or
            something unexpected. Kept for the handle; it never propagates out of the client's
            task group.
    """

    runner: JobRunner
    done: anyio.Event = dataclasses.field(default_factory=anyio.Event)
    error: BaseException | None = None


def _status(report: JobReport) -> JobStatus:
    """The backend-neutral status derived from the local report."""
    reason: str | None = None
    if report.state == "stopped" and not report.failed:
        # Why the last episode ended, when nothing of the job's own failed: what the job attempt
        # was cancelled or evicted for, else what interrupted the root.
        latest = report.job_attempts[-1] if report.job_attempts else None
        root = report.invocations.get(report.job.root)
        if latest is not None and (latest.reason or latest.evicted_reason):
            reason = latest.reason or latest.evicted_reason
        elif root is not None and root.reason is not None:
            reason = root.reason
    return JobStatus(
        job_id=report.job.id,
        state=report.state,
        result=report.result,
        errors=report.errors,
        step_errors=report.step_errors,
        reason=reason,
        cache_conflicts=report.cache_conflicts,
    )


def _jobs(n: int) -> str:
    return "1 job" if n == 1 else f"{n} jobs"


def _array_info(info: ProjectArrayInfo) -> ArrayInfo:
    return ArrayInfo(
        name=info.name,
        type=info.type,
        row_count=info.row_count,
        created_at=info.created_at,
        updated_at=info.updated_at,
    )


def _snapshot(info: SnapshotInfo) -> Snapshot:
    return Snapshot(array_name=info.array_name, array=info.array, created_at=info.created_at)


class LocalJobHandle(JobHandle):
    """A job held through a :class:`LocalProjectClient` (see :class:`~fxtr.project.client.JobHandle`
    for the contract). Beyond it, :attr:`error` says how this process's run of the job ended, if
    the client ran it and the run raised."""

    def __init__(self, client: LocalProjectClient, job_id: JobID) -> None:
        self._client = client
        self._job_id = job_id

    def __repr__(self) -> str:
        return f"LocalJobHandle({self._job_id})"

    @property
    def job_id(self) -> JobID:
        return self._job_id

    @property
    def error(self) -> BaseException | None:
        """What this process's run of the job raised, if the client ran the job and the run
        raised: a :class:`~fxtr.runner.postgres.runner.JobEvictedError` after a hard cancel or a
        takeover, or an unexpected failure of the runner. ``None`` while the run is going, when it
        ended by settling the job attempt, and for a job this client did not run."""
        run = self._client._runs.get(self._job_id)
        return None if run is None else run.error

    async def status(self) -> JobStatus:
        return _status(await self._client._report(self._job_id))

    async def wait(self) -> JobStatus:
        run = self._client._runs.get(self._job_id)
        if run is not None:
            await run.done.wait()
        # This process's part may have ended with the job still running elsewhere (a takeover), or
        # the client may never have run the job: follow the database until the job stops.
        while True:
            status = await self.status()
            if status.state != "running":
                return status
            await self._client.database.wait_for(
                job_topic(self._job_id), self._client._settings.poll_interval
            )

    async def result(self) -> Array[Any]:
        status = await self.status()
        if status.result is None:
            raise JobNotFinishedError(f"Job {self._job_id} is {status.state}")
        return await self._client.load(status.result)

    async def cancel(self, *, hard: bool = False) -> None:
        try:
            await self._client.jobs.cancel(self._job_id, hard=hard)
        except ledger.NotFoundError as exc:
            raise JobNotFoundError(*exc.args) from exc
        except execution.NotCancellableError:
            pass  # not running: nothing to cancel


class LocalProjectClient(AsyncContextManagerMixin, ProjectClient):
    """The project client that runs its jobs in this process (see the module docstring).

    Usable only as an async context manager: ``async with LocalProjectClient.from_project() as
    client``. The block is where jobs run; leaving it waits for them and releases the database.

    Args:
        database: The project's database; the client owns it and disposes of it on exit.
        project: The project, as :func:`~fxtr.project.config.locate_project` reads it: its
            declared modules, its staleness threshold, and where its database is.
        environment: What answers questions about the checkout at a launch: which code is checked
            out (pinned as a commit) and whether the venv matches the project. The local
            environment, running git, jj, and uv, by default; tests substitute a fake.
        registry: The steps and workflows this process can run; the global registry (everything
            imported so far) by default.
        limits: How much of each job runs at once.
        settings: The runner's clocks.
        verify_determinism: Whether jobs verify every workflow body's return by a second run of
            the body (see :mod:`fxtr.runner.assembly`), when a ``submit`` or ``resume`` does not
            say.
        verify_environment: Whether a launch or resume refuses when the venv does not match the
            project (see :meth:`submit`), when a ``submit`` or ``resume`` does not say.
        handle_sigint: Whether the client handles Ctrl-C for its jobs while entered (soft cancel,
            then hard, then stop); off when something else owns the signal, such as a test runner.
        create_schema: Whether entering the client creates the runner's tables if they do not
            exist yet. ``from_project`` sets it, so a first launch against a fresh database works;
            the constructor's default leaves the schema to the caller.

    Attributes:
        database: The database.
        project: The project.
        environment: The environment.
        registry: The registry.
        entities: The underlying entity store, for ``flush`` and raw reads; the client's own
            ``store`` and ``load`` go through it.
        jobs: The local inspection surface over the job state (episodes, invocations, steps).
    """

    def __init__(
        self,
        database: Database,
        *,
        project: Project,
        environment: Environment | None = None,
        registry: FunctionRegistry = GLOBAL_REGISTRY,
        limits: RunLimits = RunLimits(),
        settings: RunnerSettings = RunnerSettings(),
        verify_determinism: bool = True,
        verify_environment: bool = True,
        handle_sigint: bool = True,
        create_schema: bool = False,
    ) -> None:
        self.database = database
        self.project = project
        self.environment: Environment = LocalEnvironment() if environment is None else environment
        self.registry = registry
        self.entities = PostgresEntityStorage(database)
        self.jobs = Jobs(database, self.entities)
        self._arrays = ProjectArrays(database, self.entities)
        self._limits = limits
        self._settings = settings
        self._verify_determinism = verify_determinism
        self._verify_environment = verify_environment
        self._handle_sigint = handle_sigint
        self._create_schema = create_schema
        self._tg: anyio.abc.TaskGroup | None = None
        self._runs: dict[JobID, _Run] = {}
        self._presses = 0  # Ctrl-C presses (or interrupt() calls) while entered
        self._stopped_by_interrupt = False  # the last press cancelled the block
        self._receiver_scope: anyio.CancelScope | None = None  # while entered, the receiver's

    @classmethod
    def from_project(
        cls,
        anchor: Path | str | None = None,
        *,
        environment: Environment | None = None,
        registry: FunctionRegistry = GLOBAL_REGISTRY,
        limits: RunLimits = RunLimits(),
        settings: RunnerSettings = RunnerSettings(),
        verify_determinism: bool = True,
        verify_environment: bool = True,
        handle_sigint: bool = True,
    ) -> LocalProjectClient:
        """The client of the project a path belongs to.

        Locates the project from ``anchor`` (a launcher script passes its own ``__file__``; the
        working directory when ``None``, which is what the CLI wants), loads its ``.env`` into
        the environment (:func:`~fxtr.project.config.load_env_file`), imports the project's
        declared modules so their steps and workflows are registered, and connects to the
        project's database in its schema. The tables are created on entry if they do not exist.

        Raises:
            ProjectNotFoundError: If no fxtr project lies at or above the anchor.
            ConfigError: If the project's configuration is malformed or names no database.
            ImportError: If a declared module cannot be imported.
        """
        env: Environment = LocalEnvironment() if environment is None else environment
        project = env.locate_project(None if anchor is None else Path(anchor))
        load_env_file(project.root)
        for module in project.modules:
            importlib.import_module(module)
        return cls(
            Database(project.database_url, pg_schema=project.schema),
            project=project,
            environment=env,
            registry=registry,
            limits=limits,
            settings=settings,
            verify_determinism=verify_determinism,
            verify_environment=verify_environment,
            handle_sigint=handle_sigint,
            create_schema=True,
        )

    # -- lifetime --

    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        unwrapped: BaseException | None = None
        try:
            if self._create_schema:
                await self.database.create_schema()
            try:
                async with anyio.create_task_group() as tg:
                    self._tg = tg
                    self._receiver_scope = anyio.CancelScope()
                    if self._handle_sigint:
                        tg.start_soon(self._receive_interrupts)
                    try:
                        yield self
                    finally:
                        self._tg = None
                    # The block ended normally: wait for the jobs, then let the signal receiver
                    # go. (An exception leaves through the task group, which cancels both.)
                    for run in list(self._runs.values()):
                        await run.done.wait()
                    self._receiver_scope.cancel()
            except BaseExceptionGroup as group:
                # The group's tasks never raise (a runner's failure stays on its record), so the
                # group holds the block's own exception: hand it back as itself. Raised below,
                # outside this handler, so that its own __cause__ and __context__ survive (a
                # raise inside the handler would replace them with the group).
                if len(group.exceptions) != 1:
                    raise
                unwrapped = group.exceptions[0]
            if unwrapped is not None:
                raise unwrapped
            if self._stopped_by_interrupt:
                raise KeyboardInterrupt
        finally:
            await self.database.dispose()

    async def _receive_interrupts(self) -> None:
        """Turn each Ctrl-C into :meth:`interrupt`, for as long as the client is entered."""
        assert self._receiver_scope is not None
        with self._receiver_scope, anyio.open_signal_receiver(signal.SIGINT) as signals:
            async for _ in signals:
                self.interrupt()

    def interrupt(self) -> None:
        """What a Ctrl-C press does (see the module docstring): the first call stops every running
        job gently, the second stops their running work, the third, or a call while nothing is
        running, cancels the client's block. Prints what it did. Call it on the event loop's
        thread."""
        self._presses += 1
        live = [run for run in self._runs.values() if not run.done.is_set()]
        if self._presses == 1 and live:
            self._say(
                f"interrupted: stopping {_jobs(len(live))} gently. Nothing more starts; work"
                " already running finishes and is kept. Press Ctrl-C again to stop it now."
            )
            for run in live:
                run.runner.interrupt()
        elif self._presses == 2 and live:
            self._say(
                f"interrupted again: stopping the running work of {_jobs(len(live))} now. The"
                " jobs can be resumed."
            )
            for run in live:
                run.runner.interrupt()
        else:
            self._say("interrupted: cancelling all async tasks and stopping.")
            self._stopped_by_interrupt = True
            if self._tg is not None:
                self._tg.cancel_scope.cancel()

    @staticmethod
    def _say(message: str) -> None:
        print(f"fxtr: {message}", file=sys.stderr, flush=True)

    # -- entities: the storage contract, delegated --

    def is_known(self, entity_id: EntityID) -> bool:
        return self.entities.is_known(entity_id)

    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        return await self.entities.store_raw_data(data)

    async def load_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        return await self.entities.load_raw_data(entity_id)

    # -- project arrays --

    async def create_array(self, name: str, type: ArrayTypeSpec) -> ArrayInfo:
        return _array_info(await self._arrays.create_array(name, type))

    async def describe_array(self, name: str) -> ArrayInfo:
        return _array_info(await self._arrays.describe(name))

    async def list_arrays(self) -> Sequence[ArrayInfo]:
        return [_array_info(info) for info in await self._arrays.list_arrays()]

    async def drop_array(self, name: str) -> None:
        await self._arrays.drop_array(name)

    async def load_array(self, name: str) -> Array[Any]:
        return await self._arrays.load(name)

    async def upsert_rows(self, name: str, rows: Array[Any]) -> ArrayInfo:
        return _array_info(await self._arrays.upsert(name, rows))

    async def replace_array(self, name: str, rows: Array[Any]) -> ArrayInfo:
        return _array_info(await self._arrays.replace(name, rows))

    async def delete_rows(self, name: str, keys: Iterable[KeySpec]) -> int:
        return await self._arrays.delete_rows(name, keys)

    async def clear_array(self, name: str) -> int:
        return await self._arrays.clear(name)

    async def snapshot_array(self, name: str) -> Snapshot:
        return _snapshot(await self._arrays.snapshot(name))

    async def list_snapshots(self, name: str) -> Sequence[Snapshot]:
        return [_snapshot(info) for info in await self._arrays.list_snapshots(name)]

    # -- jobs --

    async def submit(
        self,
        workflow: Workflow[Any, Any, Any] | str,
        inputs: Mapping[str, Array[Any] | BoundID[Array[Any]]],
        *,
        root: str = "root",
        verify_determinism: bool | None = None,
        allow_staged: bool = False,
        verify_environment: bool | None = None,
        overwrite_cache_conflicts: bool = False,
    ) -> LocalJobHandle:
        """Launch a job and run it in this process (see :meth:`ProjectClient.submit` for the
        contract). In order: the workflow is looked up in the client's registry, so a resume from
        another process finds the same function; the environment is checked unless
        ``verify_environment`` is off; the code is pinned; then, in one transaction, the job is
        created with its inputs and its first job attempt taken by this process; and a runner for
        it starts in the client's task group. Nothing is written before the checks pass.

        Args:
            workflow: The root workflow, or its registered name.
            overwrite_cache_conflicts: As for :meth:`ProjectClient.submit`. Recorded on the job
                attempt that this launch creates.
            verify_environment: Whether to refuse the launch when the venv does not match the
                project (:meth:`~fxtr.project.environment.Environment.check_synced`); the
                client's default when ``None``. Recorded on the job attempt either way. Local
                to this client: the code that runs is always this process's, so this is where
                the check matters.

        Raises:
            EnvironmentNotSyncedError: If the environment was checked and does not match.
            RegistryError: If the workflow is not the one registered under its name, or a name
                is registered by nothing or by several functions.
        """
        tg = self._task_group()
        workflow = self._registered(workflow)
        code_version = await self._code_version(
            allow_staged=allow_staged, verify_environment=verify_environment
        )
        owner = default_owner()
        async with self.database.transaction() as tx:
            job_id = await self.jobs.create(workflow, inputs, root=root, tx=tx)
            taken = await execution.resume(
                tx,
                job_id,
                owner=owner,
                code_version=code_version,
                overwrite_cache_conflicts=overwrite_cache_conflicts,
            )
        assert isinstance(taken, JobAttemptRecord)  # a new job has no result
        self._start(tg, taken, verify_determinism=verify_determinism)
        return LocalJobHandle(self, job_id)

    async def resume(
        self,
        job_id: JobID,
        *,
        allow_staged: bool = False,
        verify_environment: bool | None = None,
        verify_determinism: bool | None = None,
        overwrite_cache_conflicts: bool = False,
    ) -> LocalJobHandle:
        """Take a stopped job and run what is unfinished in it here (see
        :meth:`ProjectClient.resume` for the contract). The checks and the pin are as for
        :meth:`submit`; the job's active job attempt, if another process holds one, is taken over
        once its lease has been quiet for the project's ``stale_after``, and refused before that.

        Args:
            verify_environment: As for :meth:`submit`.
            verify_determinism: As for :meth:`submit`; a resume may choose afresh, and takes the
                client's default when it does not.
            overwrite_cache_conflicts: As for :meth:`ProjectClient.resume`. Recorded on the job
                attempt that this resume creates.

        Raises:
            JobNotFoundError: If there is no such job.
            ProjectClientError: If the job has succeeded, or another process is running it.
            EnvironmentNotSyncedError: If the environment was checked and does not match.
        """
        tg = self._task_group()
        code_version = await self._code_version(
            allow_staged=allow_staged, verify_environment=verify_environment
        )
        owner = default_owner()
        try:
            async with self.database.transaction() as tx:
                await self.entities.flush(tx)  # the code version, referenced by the job attempt
                taken = await execution.resume(
                    tx,
                    job_id,
                    owner=owner,
                    code_version=code_version,
                    take_over_after=self.project.stale_after,
                    overwrite_cache_conflicts=overwrite_cache_conflicts,
                )
        except ledger.NotFoundError as exc:
            raise JobNotFoundError(*exc.args) from exc
        except execution.JobRunningElsewhereError as exc:
            raise ProjectClientError(str(exc)) from exc
        if isinstance(taken, execution.AlreadyFinished):
            raise ProjectClientError(f"Job {job_id} has succeeded; there is nothing to resume")
        self._start(tg, taken, verify_determinism=verify_determinism)
        return LocalJobHandle(self, job_id)

    async def run(
        self,
        workflow: Workflow[Any, Any, Any] | str,
        inputs: Mapping[str, Array[Any] | BoundID[Array[Any]]],
        *,
        root: str = "root",
        verify_determinism: bool | None = None,
        allow_staged: bool = False,
        verify_environment: bool | None = None,
        overwrite_cache_conflicts: bool = False,
    ) -> JobStatus:
        """:meth:`submit`, then :meth:`~LocalJobHandle.wait`, with the local ``submit``'s
        options."""
        handle = await self.submit(
            workflow,
            inputs,
            root=root,
            verify_determinism=verify_determinism,
            allow_staged=allow_staged,
            verify_environment=verify_environment,
            overwrite_cache_conflicts=overwrite_cache_conflicts,
        )
        return await handle.wait()

    async def job(self, job_id: JobID) -> LocalJobHandle:
        await self._report(job_id)  # raises JobNotFoundError
        return LocalJobHandle(self, job_id)

    async def viewer_url(self) -> str | None:
        """The project's ``fxtr view`` on this machine, when one runs
        (:func:`~fxtr.project.viewer.find_viewer`)."""
        return await anyio.to_thread.run_sync(find_viewer, self.project)

    async def list_jobs(self) -> Sequence[JobSummary]:
        summaries: list[JobSummary] = []
        for record in await self.jobs.list():
            report = await self.jobs.status(record.id)
            summaries.append(
                JobSummary(
                    job_id=record.id,
                    workflow_name=record.workflow_name,
                    root=record.root,
                    created_at=record.created_at,
                    state=report.state,
                )
            )
        return summaries

    # -- job records --

    async def export_job(self, job_id: JobID, path: Path) -> EntityID:
        async with self.database.transaction(snapshot=True) as tx:
            if await execution.active_job_attempt(tx, job_id) is not None:
                raise JobRunningError(
                    f"Job {job_id} is held by a process; export it once it has stopped (or"
                    " cancel it first)"
                )
            try:
                return await job_records.export_job(tx, self.entities, job_id, path)
            except ledger.NotFoundError as exc:
                raise JobNotFoundError(*exc.args) from exc

    async def import_job(self, path: Path, *, update: bool = False) -> JobID:
        try:
            file = job_records.read_record(path)
        except job_records.RecordFileError as exc:
            raise ImportRefusedError(f"{path}: {exc}") from exc
        missing = await self.environment.missing_commits(self.project, file.commits())
        if missing:
            raise ImportRefusedError(
                f"{path}'s results were produced at commits this project's repository does not"
                f" have ({', '.join(missing)}); fetch them first"
            )
        job_id = JobID(uuid.UUID(file.record.job.id))
        async with self.database.transaction() as tx:
            if await job_records.lock_job(tx, job_id):
                if not update:
                    raise ImportRefusedError(
                        f"Job {job_id} is already in this project; to bring it up to date from"
                        " this record, import it with update (--update)"
                    )
                if await execution.active_job_attempt(tx, job_id) is not None:
                    raise ImportRefusedError(
                        f"Job {job_id} is held by a process here; update it once it has stopped"
                    )
            try:
                return await job_records.insert_record(tx, file)
            except job_records.RecordFileError as exc:
                raise ImportRefusedError(f"{path}: {exc}") from exc

    def _task_group(self) -> anyio.abc.TaskGroup:
        """The task group jobs run in; refuses when the client is not entered or was interrupted."""
        if self._tg is None:
            raise RuntimeError(
                "LocalProjectClient runs jobs in its own task group: enter it first"
                " (async with client: ...)"
            )
        if self._presses > 0:
            raise ProjectClientError("The client was interrupted and accepts no new jobs")
        return self._tg

    def _registered(self, workflow: Workflow[Any, Any, Any] | str) -> Workflow[Any, Any, Any]:
        """The workflow as the client's registry knows it, which is what a runner here or in
        another process will execute under that name."""
        if isinstance(workflow, str):
            return self.registry.workflow(workflow)
        found = self.registry.workflow(workflow.name)
        if found is not workflow:
            raise RegistryError(
                f"The workflow registered as {workflow.name!r} is not the one given"
                f" ({found.body.__module__}.{found.body.__qualname__}, not"
                f" {workflow.body.__module__}.{workflow.body.__qualname__}); a job of it would run"
                " the registered one"
            )
        return found

    async def _code_version(
        self, *, allow_staged: bool, verify_environment: bool | None
    ) -> BoundID[CodeVersion]:
        """Check the environment (refusing on problems), pin the code, and store the code version
        this process runs (the pin, the check's outcome, and where each function the registry can
        run is) in the client's store, pending until the job's transaction flushes it."""
        if verify_environment is None:
            verify_environment = self._verify_environment
        if verify_environment:
            problems = await self.environment.check_synced(self.project)
            if problems:
                raise EnvironmentNotSyncedError(problems)
        pin = await self.environment.pin_code(self.project, allow_staged=allow_staged)
        return await self.entities.store(
            CodeVersion(
                commit=pin.commit,
                tree=pin.tree,
                ephemeral=pin.ephemeral,
                project_path=pin.project_path,
                environment_verified=verify_environment,
                functions=function_sources(self.registry, pin.repo_root),
            )
        )

    def _start(
        self,
        tg: anyio.abc.TaskGroup,
        job_attempt: JobAttemptRecord,
        *,
        verify_determinism: bool | None,
    ) -> None:
        """Start a runner for a job attempt this process has just taken, as a task of the
        client's group."""
        runner = JobRunner(
            self.database,
            job_attempt,
            execute=in_process_executor(self.registry),
            limits=self._limits,
            settings=self._settings,
            verify_determinism=(
                self._verify_determinism if verify_determinism is None else verify_determinism
            ),
        )
        run = _Run(runner)
        self._runs[job_attempt.job_id] = run
        tg.start_soon(self._run, run)

    async def _run(self, run: _Run) -> None:
        """One job's runner task. Whatever it raises stays on its record: a job's failure to run
        here is the handle's to report, not a reason to stop the client's other jobs."""
        job_id = run.runner.job_id
        try:
            settled = await run.runner.run()
            logger.info("job %s: job attempt %s settled %s", job_id, settled.id, settled.state)
        except JobEvictedError as exc:
            run.error = exc
            logger.info("job %s: %s", job_id, exc)
        except Exception as exc:
            run.error = exc
            logger.exception("job %s: the runner failed", job_id)
        finally:
            run.done.set()

    async def _report(self, job_id: JobID) -> JobReport:
        try:
            return await self.jobs.status(job_id)
        except ledger.NotFoundError as exc:
            raise JobNotFoundError(*exc.args) from exc

    # -- the step cache --

    async def list_cache_entries(
        self,
        prefix: str | None = None,
        *,
        step: str | None = None,
        state: CacheEntryState | None = None,
        conflicts_in: JobID | None = None,
    ) -> Sequence[CacheEntry]:
        async with self.database.transaction(snapshot=True) as tx:
            keys = None
            if conflicts_in is not None:
                keys = [r.cache_key for r in await _cache_conflicts(tx, conflicts_in)]
            return await execution.select_cache_entries(
                tx, prefix=prefix, keys=keys, step=step, state=state
            )

    async def clear_cache_entries(
        self,
        prefix: str | None = None,
        *,
        addresses: Iterable[str] | None = None,
        conflicts_in: JobID | None = None,
        step: str | None = None,
        state: CacheEntryState | None = None,
    ) -> int:
        if sum(given is not None for given in (prefix, addresses, conflicts_in)) != 1:
            raise ValueError("give exactly one of prefix, addresses, and conflicts_in")
        keys = addresses
        async with self.database.transaction() as tx:
            # For a conflict, clear the entry only while it offers the result that is in the way.
            offering: dict[str, StepResultID] = {}
            if conflicts_in is not None:
                offering = {
                    r.cache_key: r.offered_step_result_id
                    for r in await _cache_conflicts(tx, conflicts_in)
                    if r.offered_step_result_id is not None
                }
                keys = offering
            candidates = await execution.select_cache_entries(
                tx,
                prefix=prefix,
                keys=None if keys is None else set(keys),
                step=step,
                state=state,
            )
            cleared = 0
            for entry in candidates:
                if await execution.clear_cache_entry(
                    tx, entry.cache_address, offering=offering.get(entry.cache_address)
                ):
                    cleared += 1
        return cleared

    async def adopt_cache(
        self,
        job_id: JobID,
        prefix: str | None = None,
        *,
        overwrite_cache_conflicts: bool = False,
    ) -> AdoptReport:
        adopted: list[str] = []
        skipped: dict[str, AdoptSkipReason] = {}
        pattern = None if prefix is None else parse_pattern(prefix)
        async with self.database.transaction() as tx:
            try:
                await ledger.get_job(tx, job_id)
            except ledger.NotFoundError as exc:
                raise JobNotFoundError(*exc.args) from exc
            bindings = sorted(
                await ledger.list_step_bindings(tx, job_id), key=lambda b: b.cache_key
            )
            for binding in bindings:
                if pattern is not None and not pattern.selects(binding.cache_key):
                    continue
                step_result = await ledger.get_step_result(tx, binding.step_result_id)
                outcome = await execution.adopt_step_result(
                    tx,
                    binding.cache_key,
                    step_result,
                    overwrite_cache_conflicts=overwrite_cache_conflicts,
                )
                if outcome == "adopted":
                    adopted.append(binding.cache_key)
                else:
                    skipped[binding.cache_key] = outcome
        return AdoptReport(adopted, skipped)


async def _cache_conflicts(tx: Transaction, job_id: JobID) -> list[StepStateRecord]:
    """Get the cache conflicts of a job, as
    :func:`~fxtr.runner.postgres.execution.cache_conflicts` does.

    Raises:
        JobNotFoundError: If there is no such job.
    """
    try:
        return await execution.cache_conflicts(tx, job_id)
    except ledger.NotFoundError as exc:
        raise JobNotFoundError(*exc.args) from exc


__all__ = ["EnvironmentNotSyncedError", "LocalJobHandle", "LocalProjectClient"]
