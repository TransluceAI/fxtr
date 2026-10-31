"""The project client: how a launcher script or an application talks to its project.

A :class:`ProjectClient` is the whole of what user code holds when it works with a project, the
way a :class:`~fxtr.experiment.workflows.WorkflowContext` is the whole of what a workflow body
holds: one object that is the project's entity store and, on the same object, the project's
arrays, jobs, and step cache. Nothing behind it is visible through it.

**Entities.** A client is an :class:`~fxtr.core.entities.FxtrEntityStorage`: ``store`` and
``load`` (and the raw pair) work as on any engine, against the project's store. Inputs to a job
are built by storing entities and arrays here.

**Project arrays.** Named, typed arrays the user edits over time (``create_array``,
``upsert_rows``, ...). A job never reads one by name; a launch takes a *snapshot*
(``snapshot_array``), an ``Array`` entity of the rows as they are, and passes its bound id as a
job input like any other array. Which array an input came from, and what its rows were, is then
derivable from the snapshot's content id.

**Jobs.** One launch verb, ``submit``, for every client: it creates a job of a root workflow on
its inputs and returns a :class:`JobHandle`, through which the job is watched (``status``),
waited for (``wait``), read (``result``), and cancelled. ``resume`` returns a handle to a job that
stopped, asking for what is unfinished in it to be finished; ``job`` attaches to any existing
job; ``run`` is ``submit`` then ``wait``. What ``submit`` does after creating the job is
the client's own business: a local client runs the job in the submitting process, a remote one
sends it elsewhere.

**Code.** Every launch and resume pins the code it runs, as a commit of the project's repository
(:mod:`fxtr.project.environment`); ``allow_staged`` is how a launch pins staged but uncommitted
changes as a commit of fxtr's own. The pin is recorded on the job attempt the process creates.

**Cache.** Step results are cached across jobs by cache address, a ``/``-joined path (the run
node's prefix with the mapped key appended, such as ``root/refine[bio:"ada"]/rewrite``).
``clear_cache_entries`` is how a user whose step changed behaviour makes the next job run it
afresh: by address prefix, by step, or by explicit addresses. The cache methods are provisional:
their selection arguments cover the operations known to be wanted and may grow. A job is in *cache
conflict* at a key if its request is different from the request that owns the entry at that key.
The owner is the completed result or the running attempt of another job. The status of a job
lists its cache conflicts (:attr:`JobStatus.cache_conflicts`).

The records here (:class:`JobSummary`, :class:`JobStatus`) say what every client can say about a
job. What a particular client knows beyond that, such as the local client's episodes and
leases, is exposed on that client, not here.
"""

from __future__ import annotations

import abc
import dataclasses
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NewType

from fxtr.core.entities import BoundID, FxtrEntityStorage
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ArrayType, ArrayTypeSpec, KeySpec
from fxtr.experiment.workflows import Workflow
from fxtr.runner.session import AttemptError

JobID = NewType("JobID", uuid.UUID)
"""A job's identity, minted by the client that created it."""

type JobState = Literal["running", "succeeded", "stopped"]
"""Where a job is: ``running`` while a process works on it, ``succeeded`` once its root has its
result, and otherwise ``stopped``: nothing is working on it and it has no result, including a job
no process has taken yet. A stopped job with errors failed; one without was interrupted,
cancelled, or never taken. Resuming a stopped job continues it."""


class ProjectClientError(Exception):
    """An error raised by a project client's own rules (not by user code in a job)."""


class JobNotFoundError(ProjectClientError, KeyError):
    """No job has the given id."""


class JobNotFinishedError(ProjectClientError):
    """The job has no result yet."""


class ImportRefusedError(ProjectClientError):
    """A job record cannot be imported here: the file is not a record this fxtr reads, it names
    code this repository does not have, the job is already here, or it disagrees with this
    project about a fact both have."""


class JobRunningError(ProjectClientError):
    """The job is held by a process (it has an active job attempt), and the operation needs it
    settled."""


# ---- records ----


@dataclasses.dataclass(frozen=True)
class ArrayInfo:
    """What the project records about one of its arrays.

    Attributes:
        name: The array's name.
        type: Its declared type.
        row_count: How many rows it holds now.
        created_at: When it was created.
        updated_at: When its rows last changed (its creation time until then).
    """

    name: str
    type: ArrayType
    row_count: int
    created_at: datetime
    updated_at: datetime


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """A project array's rows as they were when the snapshot was taken, as a stored ``Array``
    entity. Snapshots of unchanged rows share one entity, so equal ``array`` ids mean equal rows.

    Attributes:
        array_name: The array.
        array: The snapshot entity's bound id: what a launch passes as a job input.
        created_at: When it was taken.
    """

    array_name: str
    array: BoundID[Array[Any]]
    created_at: datetime


@dataclasses.dataclass(frozen=True)
class JobSummary:
    """One job in a listing.

    Attributes:
        job_id: The job.
        workflow_name: The root workflow's name.
        root: The root invocation's pathname.
        created_at: When it was created.
        state: Where it is.
    """

    job_id: JobID
    workflow_name: str
    root: str
    created_at: datetime
    state: JobState


@dataclasses.dataclass(frozen=True)
class JobStatus:
    """Where a job is and what, if anything, went wrong.

    Attributes:
        job_id: The job.
        state: Where it is.
        result: The root's result array, once ``succeeded``.
        errors: What failed among its workflow invocations, by pathname.
        step_errors: What failed among the steps it requested, by cache address.
        reason: Why the job's last episode ended, when something other than its own work ended
            it: it was cancelled, the process running it stopped, or another process took it
            over. ``None`` while the job is ``running``, once it has ``succeeded``,
            and for a job that stopped because its work failed (``errors`` and ``step_errors``
            say why then).
        cache_conflicts: The addresses where the request of the job is in cache conflict, in address
            order. The list is there in all job states. For a stopped job, it is what the job
            waits on. For a running job, it is what the job will wait on.
    """

    job_id: JobID
    state: JobState
    result: BoundID[Array[Any]] | None
    errors: Mapping[str, AttemptError]
    step_errors: Mapping[str, AttemptError]
    reason: str | None
    cache_conflicts: Sequence[CacheConflict]

    @property
    def failed(self) -> bool:
        """Stopped with at least one failure."""
        return self.state == "stopped" and bool(self.errors or self.step_errors)


type CacheConflictKind = Literal["completed", "running"]
"""What owns a key where a job is in cache conflict. ``completed``: the completed result of a
different request. If you clear the key, you remove this result. ``running``: an attempt of a
different request, which runs now."""


@dataclasses.dataclass(frozen=True)
class CacheConflict:
    """A key where the request of a job is different from the request that owns the cache
    entry. The job cannot run its request at this key until the entry is cleared.

    Attributes:
        cache_address: The cache address.
        step_name: The step of the request that owns the entry. This is not always the step of
            the job's own request.
        kind: What owns the entry.
    """

    cache_address: str
    step_name: str
    kind: CacheConflictKind


type AdoptSkipReason = Literal["already", "equal_request", "running", "conflict"]
"""Why :meth:`ProjectClient.adopt_cache` did not adopt a result at a key. ``already``: the entry
offers that result already. ``equal_request``: the entry offers a different result of the same
request, for example a different sample. ``running``: an attempt runs at the key now.
``conflict``: the entry offers a result of a different request, and the call did not overwrite
it."""


@dataclasses.dataclass(frozen=True)
class AdoptReport:
    """What :meth:`ProjectClient.adopt_cache` did.

    Attributes:
        adopted: The addresses whose entries now offer the results of the job, in address order.
        skipped: The other addresses of the job's results, with why each was not adopted, in address
            order.
    """

    adopted: Sequence[str]
    skipped: Mapping[str, AdoptSkipReason]


type CacheEntryState = Literal["completed", "running", "failed", "abandoned"]
"""What a cache entry holds: a result, a step attempt still computing one, a failure, or an
attempt its process abandoned (the next job to request the key runs the step again)."""


@dataclasses.dataclass(frozen=True)
class CacheEntry:
    """One entry of the step cache (provisional; see the module docstring).

    Attributes:
        cache_address: The cache address (:mod:`fxtr.runner.steps`).
        step_name: The step it was computed by.
        state: What it holds.
    """

    cache_address: str
    step_name: str
    state: CacheEntryState


# ---- the handle ----


class JobHandle(abc.ABC):
    """A job, held: the one way to watch, wait for, read, or cancel a job, whichever client made
    it. A handle holds no state of its own beyond the job's id; every method asks the project."""

    @property
    @abc.abstractmethod
    def job_id(self) -> JobID:
        """The job."""

    @abc.abstractmethod
    async def status(self) -> JobStatus:
        """Where the job is now.

        Raises:
            JobNotFoundError: If the job no longer exists.
        """

    @abc.abstractmethod
    async def wait(self) -> JobStatus:
        """Wait until the job is no longer ``running``, and return its status.

        Returns as soon as the job is ``succeeded`` or ``stopped``; a job no process has taken is
        ``stopped``, so waiting on it returns at once.
        """

    @abc.abstractmethod
    async def result(self) -> Array[Any]:
        """The root workflow's result array.

        Raises:
            JobNotFinishedError: If the job has not ``succeeded`` (waits for nothing).
        """

    @abc.abstractmethod
    async def cancel(self, *, hard: bool = False) -> None:
        """Ask for the job to stop. Soft (the default) lets what is running finish on its own
        terms and starts nothing more; hard stops what is running too. A cancelled job is
        ``stopped`` once its process has complied, and can be resumed. A no-op on a job that is
        not running."""


# ---- the client ----


class ProjectClient(FxtrEntityStorage, abc.ABC):
    """A project, as user code talks to it (see the module docstring).

    Entity storage is inherited: the client is the project's entity store. The methods below are
    the arrays, the jobs, and the cache. Keyword-only arguments follow the positional essentials
    everywhere, so a client may add options of its own after them.
    """

    # -- project arrays --

    @abc.abstractmethod
    async def create_array(self, name: str, type: ArrayTypeSpec) -> ArrayInfo:
        """Declare a project array with a type. Declaring an existing array again with the same
        type is a no-op; with another type it is an error (drop and create instead).

        Raises:
            ProjectClientError: If the name exists with another type.
        """

    @abc.abstractmethod
    async def describe_array(self, name: str) -> ArrayInfo:
        """What the project records about the array.

        Raises:
            KeyError: If there is no such array.
        """

    @abc.abstractmethod
    async def list_arrays(self) -> Sequence[ArrayInfo]:
        """Every project array, by name."""

    @abc.abstractmethod
    async def drop_array(self, name: str) -> None:
        """Remove the array, its rows, and its snapshot records; the snapshot entities remain.

        Raises:
            KeyError: If there is no such array.
        """

    @abc.abstractmethod
    async def load_array(self, name: str) -> Array[Any]:
        """The array's current rows, as an ``Array`` of its declared type.

        Raises:
            KeyError: If there is no such array.
        """

    @abc.abstractmethod
    async def upsert_rows(self, name: str, rows: Array[Any]) -> ArrayInfo:
        """Insert or replace rows by key. ``rows`` must have the array's type; entities its values
        reference must be stored (pending stores through this client are persisted with it).

        Raises:
            KeyError: If there is no such array.
            ProjectClientError: If ``rows`` has another type.
        """

    @abc.abstractmethod
    async def replace_array(self, name: str, rows: Array[Any]) -> ArrayInfo:
        """Replace every row with ``rows``, as :meth:`upsert_rows` after clearing."""

    @abc.abstractmethod
    async def delete_rows(self, name: str, keys: Iterable[KeySpec]) -> int:
        """Delete the rows at ``keys`` and return how many existed.

        Raises:
            KeyError: If there is no such array.
        """

    @abc.abstractmethod
    async def clear_array(self, name: str) -> int:
        """Delete every row and return how many there were.

        Raises:
            KeyError: If there is no such array.
        """

    @abc.abstractmethod
    async def snapshot_array(self, name: str) -> Snapshot:
        """The array's rows as they are now, as a stored ``Array`` entity: what a launch passes
        as a job input. Snapshotting an array whose rows have not changed since its last snapshot
        returns that snapshot again.

        Raises:
            KeyError: If there is no such array.
        """

    @abc.abstractmethod
    async def list_snapshots(self, name: str) -> Sequence[Snapshot]:
        """Every snapshot of the array, oldest first.

        Raises:
            KeyError: If there is no such array.
        """

    # -- jobs --

    @abc.abstractmethod
    async def submit(
        self,
        workflow: Workflow[Any, Any, Any] | str,
        inputs: Mapping[str, Array[Any] | BoundID[Array[Any]]],
        *,
        root: str = "root",
        verify_determinism: bool | None = None,
        allow_staged: bool = False,
        overwrite_cache_conflicts: bool = False,
    ) -> JobHandle:
        """Launch a job of ``workflow`` on ``inputs`` and return its handle.

        Creates the job with its root invocation and its inputs as ledger facts, pins the code the
        job runs (:mod:`fxtr.project.environment`), and arranges for it to run: in this process,
        elsewhere, or later, as the client sees fit. Returns once the job exists; ``wait`` on the
        handle is how a caller blocks on it.

        Args:
            workflow: The root workflow, which must be registered under its name
                (:mod:`fxtr.experiment.registry`), so that a resume can find it; or that name.
            inputs: Its arguments, by name: arrays, which are stored, or bound ids of stored
                arrays such as a snapshot's.
            root: The root invocation's pathname and default cache address prefix. Jobs of different
                root workflows that share one step cache pick distinct roots.
            verify_determinism: Whether every workflow body's return is held until a second run
                of the body has done the same things (see :mod:`fxtr.runner.assembly`); the
                client's default when ``None``.
            allow_staged: Whether staged but uncommitted changes in the checkout may be pinned as
                a commit of fxtr's own.
            overwrite_cache_conflicts: If ``True``, this launch overwrites the cache conflicts it
                finds with completed results. The step runs at such a key, and its new result
                replaces the entry. Jobs that already took the old result keep it. A conflict with
                a running attempt is not overwritten. A later resume does not overwrite unless it
                asks again.

        Raises:
            SignatureError: If the inputs do not fit the workflow's signature.
            RegistryError: If the workflow is not registered.
            PinRefusedError: If the checkout cannot be pinned as it is.
        """

    @abc.abstractmethod
    async def resume(
        self,
        job_id: JobID,
        *,
        allow_staged: bool = False,
        overwrite_cache_conflicts: bool = False,
    ) -> JobHandle:
        """Take a stopped job again, asking for everything unfinished in it to be finished, and
        return its handle. A resume pins the code it runs like a launch does; it may be at another
        commit than the launch.

        Args:
            job_id: The job.
            allow_staged: As for :meth:`submit`.
            overwrite_cache_conflicts: As for :meth:`submit`, for this resume only. The steps
                after an overwritten key can find conflicts of their own. This resume overwrites
                those too.

        Raises:
            JobNotFoundError: If there is no such job.
            ProjectClientError: If the job has succeeded already, or another process is running
                it.
            PinRefusedError: If the checkout cannot be pinned as it is.
        """

    @abc.abstractmethod
    async def job(self, job_id: JobID) -> JobHandle:
        """A handle to an existing job.

        Raises:
            JobNotFoundError: If there is no such job.
        """

    @abc.abstractmethod
    async def list_jobs(self) -> Sequence[JobSummary]:
        """Every job, newest first."""

    # -- job records --

    @abc.abstractmethod
    async def export_job(self, job_id: JobID, path: Path) -> EntityID:
        """Write the job's record (:mod:`fxtr.project.job_record`) and every entity it reaches to
        ``path``, as a gzipped CAR file whose root is the record, and return the record's id.

        Raises:
            JobNotFoundError: If there is no such job.
            JobRunningError: If a process holds the job; a record is of a settled job (succeeded,
                or stopped).
        """

    @abc.abstractmethod
    async def import_job(self, path: Path, *, update: bool = False) -> JobID:
        """Add the job a record file (written by :meth:`export_job`) holds to this project, with
        the entities it reaches, and return its id. The job arrives settled, as it was exported:
        ``succeeded``, or ``stopped`` and resumable here. Facts it shares with this project (the
        producers and results of steps another job here also took) must agree. The step cache is
        left as it is.

        Args:
            path: The record file.
            update: Whether a job this project has already may be brought up to date from a later
                record of it. The record must contain everything this project knows of the job
                (each of its job attempts, each of its facts, equal); what it adds is added, and
                nothing is removed. Without it, a job this project has is refused.

        Raises:
            ImportRefusedError: If the file is not a job record this fxtr reads; if a commit its
                results were produced at is not in this project's repository; if the job is
                already here and ``update`` is not given; if, with ``update``, a process holds the
                job here or the record lacks something this project knows of it; or if a fact it
                shares with this project differs.
        """

    async def run(
        self,
        workflow: Workflow[Any, Any, Any] | str,
        inputs: Mapping[str, Array[Any] | BoundID[Array[Any]]],
        *,
        root: str = "root",
        verify_determinism: bool | None = None,
        allow_staged: bool = False,
        overwrite_cache_conflicts: bool = False,
    ) -> JobStatus:
        """:meth:`submit`, then :meth:`~JobHandle.wait`."""
        handle = await self.submit(
            workflow,
            inputs,
            root=root,
            verify_determinism=verify_determinism,
            allow_staged=allow_staged,
            overwrite_cache_conflicts=overwrite_cache_conflicts,
        )
        return await handle.wait()

    async def viewer_url(self) -> str | None:
        """Where the project's viewer runs now, the URL a job's link starts with
        (:func:`~fxtr.project.viewer.job_url`), or ``None`` when this client knows of none
        running: where a launch shows its job. ``None`` unless a client knows better."""
        return None

    # -- the step cache (provisional) --

    @abc.abstractmethod
    async def list_cache_entries(
        self,
        prefix: str | None = None,
        *,
        step: str | None = None,
        state: CacheEntryState | None = None,
        conflicts_in: JobID | None = None,
    ) -> Sequence[CacheEntry]:
        """List the entries of the step cache, oldest first. Provisional: the selection arguments
        can grow.

        Each selection argument that is not ``None`` narrows the list.

        Args:
            prefix: Only the entries whose cache address is this path or is under it.
                ``"root/refine"`` selects ``root/refine[bio:"ada"]/rewrite``. A mapped value can
                be ``*``, which matches any one value: ``root/refine[bio:*]/rewrite`` selects
                that key for each ``bio``. Give all the dims of the group, in the order of the
                key.
            step: Only the entries computed by the step of this registered name.
            state: Only the entries in this state.
            conflicts_in: Only the entries that this job is in cache conflict with
                (:attr:`JobStatus.cache_conflicts`). Completed and running entries both show.

        Raises:
            JobNotFoundError: If ``conflicts_in`` names no job.
            ValueError: If ``prefix`` has a ``*`` in a place other than a mapped value.
        """

    @abc.abstractmethod
    async def adopt_cache(
        self,
        job_id: JobID,
        prefix: str | None = None,
        *,
        overwrite_cache_conflicts: bool = False,
    ) -> AdoptReport:
        """Make the cache offer the step results that a job took, so that later jobs reuse them.

        For each step result that the job is bound to, the entry at its key then offers that
        result, unless something live or equal is there. The job can be a local job or an
        imported one. Adoption keeps the provenance: a later job that takes an adopted result
        names the job attempt that produced it.

        At each key, the entry decides what happens:

        * No entry, or an entry whose attempt failed or was abandoned: the result is adopted.
        * The entry offers that result already: skipped as ``already``.
        * The entry offers a different result of the same request: skipped as
          ``equal_request``.
        * An attempt runs at the key: skipped as ``running``.
        * The entry offers a result of a different request: skipped as ``conflict``, unless
          ``overwrite_cache_conflicts`` is ``True``. Then the result is adopted.

        Args:
            job_id: The job whose results to adopt.
            prefix: Only the addresses at or under this prefix. All addresses of the job if
                ``None``. A mapped value can be ``*``, as for :meth:`list_cache_entries`.
            overwrite_cache_conflicts: If ``True``, replace an entry that offers a result of a
                different request.

        Returns:
            The adopted addresses, and the skipped addresses with why each was skipped.

        Raises:
            JobNotFoundError: If there is no such job.
            ValueError: If ``prefix`` has a ``*`` in a place other than a mapped value.
        """

    @abc.abstractmethod
    async def clear_cache_entries(
        self,
        prefix: str | None = None,
        *,
        addresses: Iterable[str] | None = None,
        conflicts_in: JobID | None = None,
        step: str | None = None,
        state: CacheEntryState | None = None,
    ) -> int:
        """Forget cache entries, so that the next job that requests them runs the steps again.
        Jobs whose requests are already bound to results keep those results. Provisional: the
        selection arguments can grow.

        Give exactly one of ``prefix``, ``addresses``, and ``conflicts_in``. It selects the
        candidate entries. Then ``step`` and ``state`` narrow the candidates, as for
        :meth:`list_cache_entries`.

        Args:
            prefix: The entries at or under this key prefix. ``""`` selects all entries. A
                mapped value can be ``*``, as for :meth:`list_cache_entries`.
            addresses: Exactly the entries at these addresses.
            conflicts_in: The completed entries that this job is in cache conflict with. This
                does not clear running conflicts: another job computes them now. It clears only
                the conflicts that the job has now. When the job resumes, its steps after the
                cleared addresses can find new conflicts.
            step: Only the entries computed by the step of this registered name.
            state: Only the entries in this state.

        Returns:
            How many entries were cleared.

        Raises:
            ValueError: If not exactly one of ``prefix``, ``addresses``, and ``conflicts_in`` is
                given, or if ``prefix`` has a ``*`` in a place other than a mapped value.
            JobNotFoundError: If ``conflicts_in`` names no job.
        """


__all__ = [
    "AdoptReport",
    "AdoptSkipReason",
    "ArrayInfo",
    "CacheConflict",
    "CacheConflictKind",
    "CacheEntry",
    "CacheEntryState",
    "JobHandle",
    "JobID",
    "JobNotFinishedError",
    "ImportRefusedError",
    "JobRunningError",
    "JobNotFoundError",
    "JobState",
    "JobStatus",
    "JobSummary",
    "ProjectClient",
    "ProjectClientError",
    "Snapshot",
]
