"""Job commands: creating, watching, inspecting, cancelling, and resuming jobs.

This is the use-case layer over the ledger and the execution state: what the local client's
``submit``, ``resume``, and handles are built on, and what it exposes as ``client.jobs`` for
inspection (the viewer reads it). Each command is one or a few short transactions composing ledger
operations with the execution policy.

**Status is derived, not stored.** :meth:`Jobs.status` (a :class:`JobReport`) reads the job's
job attempts, invocations, and attempts and reports each invocation as one of: ``running`` (it
has a live attempt), ``queued`` (the job's active job attempt is due to run it),
``completed`` (its result is materialized), ``failed`` (its latest attempt failed, with the error),
``interrupted`` (its latest attempt was interrupted, with the reason), or ``stopped`` (incomplete
and idle, with no attempt under the current episode to explain why: nothing will run it until a
resume). The job is ``succeeded`` when its root is completed, ``running`` when any invocation is
running or queued, and otherwise ``stopped``, a job nothing has taken included; a "failed" job is a
stopped job with a failed invocation or step. A step still running when the job stops (a soft
cancel) does not keep it running: nothing of the job waits on it until a resume. The job attempts
say how the job is being executed, who owns it, and whether it was cancelled (see
:mod:`fxtr.runner.postgres.execution`). Each requested step's state comes from the step relation
(:mod:`fxtr.runner.postgres.status_queries`) in one query for the whole job, so a status costs the
same few queries however many steps the job has; :meth:`Jobs.describe` is what loads each step's
attempt, logs, and provenance.

**Provenance is read from the ledger.** Which code ran something is a job attempt's fact
(``job_attempt_provenance``) with its code version (:class:`Provenance`). An invocation's result
names the job attempt that produced it; a step's binding names a step result, which names the
job attempt that produced it (another job's, for a cache hit, and possibly one whose job this
database does not have); an attempt ran under its owner job attempt. The descriptions carry each
attempt's provenance and, on steps and invocations, the result's.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.experiment.workflows import Workflow
from fxtr.project.client import CacheConflict, JobState
from fxtr.project.code_version import CodeVersion
from fxtr.runner.postgres import execution, ledger
from fxtr.runner.postgres.database import Database, Transaction
from fxtr.runner.postgres.entities import PostgresEntityStorage
from fxtr.runner.postgres.execution import (
    AttemptRecord,
    InvocationState,
    JobAttemptRecord,
    TargetStatus,
    job_topic,
)
from fxtr.runner.postgres.ledger import (
    EventRecord,
    InvocationID,
    InvocationRecord,
    JobAttemptID,
    JobAttemptProvenanceRecord,
    JobID,
    JobRecord,
    JobStepRequestRecord,
    NodeRecord,
    SpawnRecord,
    StepBindingRecord,
    StepRequestRecord,
    StepResultID,
)
from fxtr.runner.postgres.logs import LogRecord, list_logs, list_logs_many
from fxtr.runner.postgres.status_queries import StatusQueries, StepState, StepStateRecord
from fxtr.runner.session import AttemptError, AttemptID


@dataclasses.dataclass(frozen=True)
class InvocationStatus:
    """One invocation's state (see the module docstring).

    Attributes:
        pathname: The invocation.
        workflow_name: The workflow it runs.
        state: See :data:`~fxtr.runner.postgres.execution.InvocationState`.
        attempt_id: The live attempt (``queued`` or ``running``), or the latest attempt
            otherwise, if any.
        error: For ``failed``.
        reason: For ``interrupted``.
    """

    pathname: str
    workflow_name: str
    state: InvocationState
    attempt_id: AttemptID | None = None
    error: AttemptError | None = None
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class StepStatus:
    """The state of one step the job requested (see :data:`StepState`).

    Attributes:
        cache_key: The step's cache key.
        step_name: The step it runs.
        state: See :data:`StepState`.
        attempt_id: The attempt that answers for the step: the one that produced the step result
            the job's request is bound to (or that the cache entry offers), the failed one, or the
            one its active job attempt waits on; ``None`` when none, or when the producing attempt
            is not in this database.
        error: For ``failed``, what the step raised.
        cache_conflict: For ``conflict``, what owns the key. ``None`` in the other states.
    """

    cache_key: str
    step_name: str
    state: StepState
    attempt_id: AttemptID | None = None
    error: AttemptError | None = None
    cache_conflict: CacheConflict | None = None


@dataclasses.dataclass(frozen=True)
class JobReport:
    """A job's derived state in full (see the module docstring): what the local client knows
    beyond the backend-neutral :class:`~fxtr.project.client.JobStatus` it derives from this.

    Attributes:
        job: The job.
        state: See :data:`~fxtr.project.client.JobState`; the module docstring says how it is
            derived here.
        job_attempts: The job's job attempts, oldest first (the last is the current one).
        invocations: Every invocation's status, by pathname.
        steps: Every step the job requested, by cache key.
        result: The root's result, once completed.
    """

    job: JobRecord
    state: JobState
    job_attempts: list[JobAttemptRecord]
    invocations: dict[str, InvocationStatus]
    steps: dict[str, StepStatus]
    result: BoundID[Array[Any]] | None

    @property
    def job_attempt(self) -> JobAttemptRecord:
        """The current job attempt: the latest one (a job always has at least one)."""
        return self.job_attempts[-1]

    @property
    def failed(self) -> bool:
        """Stopped with at least one failed invocation or step."""
        return self.state == "stopped" and (
            any(s.state == "failed" for s in self.invocations.values())
            or any(s.state == "failed" for s in self.steps.values())
        )

    @property
    def errors(self) -> dict[str, AttemptError]:
        """The failed invocations' errors, by pathname."""
        return {path: s.error for path, s in self.invocations.items() if s.error is not None}

    @property
    def step_errors(self) -> dict[str, AttemptError]:
        """The failed steps' errors, by cache key."""
        return {key: s.error for key, s in self.steps.items() if s.error is not None}

    @property
    def cache_conflicts(self) -> list[CacheConflict]:
        """The cache conflicts of the requested steps, in address order."""
        return sorted(
            (s.cache_conflict for s in self.steps.values() if s.cache_conflict is not None),
            key=lambda conflict: conflict.cache_address,
        )


@dataclasses.dataclass(frozen=True)
class Provenance:
    """Which code ran something: the job attempt whose process ran it, as the ledger records it
    (which this database may not have run itself), with its code version loaded.

    Attributes:
        job_attempt: The job attempt; another job's, for a step result reused from the cache.
        code_version: The code its process ran.
    """

    job_attempt: JobAttemptProvenanceRecord
    code_version: CodeVersion


@dataclasses.dataclass(frozen=True)
class AttemptDescription:
    """One attempt with its event log, free-form logs, and provenance.

    Attributes:
        attempt: The attempt.
        events: What its body did, in order.
        logs: The free-form lines its body recorded through ``ctx.log``, in order.
        provenance: The code it ran.
    """

    attempt: AttemptRecord
    events: list[EventRecord]
    logs: list[LogRecord]
    provenance: Provenance


@dataclasses.dataclass(frozen=True)
class InvocationDescription:
    """One invocation with everything recorded about it.

    Attributes:
        invocation: The invocation.
        nodes: Its DAG, in creation order.
        spawns: What its run nodes expanded into.
        attempts: Its attempts, oldest first, each with its events.
        result_provenance: The code that produced its result, as the ledger records it with the
            result (``invocation.job_attempt_id``); ``None`` until there is one.
    """

    invocation: InvocationRecord
    nodes: list[NodeRecord]
    spawns: list[SpawnRecord]
    attempts: list[AttemptDescription]
    result_provenance: Provenance | None


@dataclasses.dataclass(frozen=True)
class StepDescription:
    """One step the job requested: the request, what it is bound to or waiting on.

    Attributes:
        request: The job's request at the key.
        spec: The queryable fields of that request (the step); the inputs are in the request
            entity, loadable by the fingerprint.
        binding: The result the request is bound to, once the job took a step attempt's result.
        status: What the key is to this job now, as the execution policy judges it
            (:func:`~fxtr.runner.postgres.execution.step_status`): the same answer the job's own
            evaluator gets.
        display: The same judgement named for display, with its answering attempt and error.
        attempt: The attempt behind what the step shows: the one that produced the bound step
            result (else the one that produced the step result the cache entry offers), when
            this database ran it; else the cache entry's current attempt; else ``None``.
        logs: The free-form lines ``attempt`` recorded through ``ctx.log``, in order (empty when
            there is no attempt).
        provenance: The code behind what the step shows: the job attempt that produced the bound
            (else the offered) step result, as the ledger records it, whether or not this
            database has the attempt; else the code the current attempt runs; else ``None``.
    """

    request: JobStepRequestRecord
    spec: StepRequestRecord
    binding: StepBindingRecord | None
    status: TargetStatus
    display: StepStatus
    attempt: AttemptRecord | None
    logs: list[LogRecord]
    provenance: Provenance | None

    @property
    def result_provenance(self) -> Provenance | None:
        """The code that produced the step result the request is bound to, which for a cache hit
        is another job's; ``None`` until bound."""
        return self.provenance if self.binding is not None else None


@dataclasses.dataclass(frozen=True)
class JobDescription:
    """Everything recorded about a job: its job attempts, its invocations with their nodes and
    attempts (each attempt saying which job attempt scheduled it), and the steps it requested."""

    job: JobRecord
    job_attempts: list[JobAttemptRecord]
    invocations: dict[str, InvocationDescription]
    steps: dict[str, StepDescription]


@dataclasses.dataclass(frozen=True)
class InvocationDetail:
    """One invocation with everything recorded about it and its derived status.

    Attributes:
        description: Everything recorded about the invocation.
        status: Its derived state.
        child_pathnames: The pathname of each invocation its run nodes spawned, by child id, so a
            spawn can be linked to the child it created.
        child_results: The result each spawned child invocation produced (``None`` until finished),
            by child id.
        step_results: The result each spawned step is bound to (``None`` until bound), by cache key.
        child_states: The derived state of each spawned child invocation, by child id.
        step_states: The derived state of each spawned step, by cache key.
    """

    description: InvocationDescription
    status: InvocationStatus
    child_pathnames: dict[InvocationID, str]
    child_results: dict[InvocationID, BoundID[Array[Any]] | None]
    step_results: dict[str, BoundID[Array[Any]] | None]
    child_states: dict[InvocationID, InvocationState]
    step_states: dict[str, StepState]


@dataclasses.dataclass(frozen=True)
class StepDetail:
    """One step the job requested with its derived state.

    Attributes:
        description: The request, and what it is bound to or waiting on.
        status: Its derived state.
    """

    description: StepDescription
    status: StepStatus


class Jobs:
    """Job commands (see the module docstring).

    Args:
        database: The runner's database.
        entities: The session's entity store, through which a job's input arrays are stored.
    """

    def __init__(self, database: Database, entities: PostgresEntityStorage) -> None:
        self._database = database
        self._entities = entities

    async def create(
        self,
        workflow: Workflow[Any, Any, Any],
        inputs: Mapping[str, Array[Any] | BoundID[Array[Any]]],
        *,
        root: str = "root",
        tx: Transaction | None = None,
    ) -> JobID:
        """Create a job running ``workflow`` on ``inputs``: the job and its root invocation, as
        ledger facts. Nothing runs until a process takes the job (a
        :class:`~fxtr.runner.postgres.runner.JobRunner`, through
        :func:`~fxtr.runner.postgres.execution.resume`), which creates its first job attempt;
        the local client's ``submit`` does both in one transaction.

        Args:
            workflow: The root workflow.
            inputs: Its arguments, by name. Each is an array, which this call stores, or the bound
                id of an array entity already in the store (a project array snapshot's,
                typically), which the root's input node references as it is.
            root: The root invocation's pathname and default cache key prefix.
            tx: The transaction to create the job in, flushing the store's pending entities into
                it first; the call opens one of its own when ``None``.

        Raises:
            SignatureError: If the inputs do not fit the workflow's signature.
            EntityNotFoundError: If a bound id is not in the store.
        """
        arrays = {
            name: await self._entities.load(given) if isinstance(given, BoundID) else given
            for name, given in inputs.items()
        }
        # Raises SignatureError if the inputs do not fit.
        workflow.signature.output_type({name: array.type for name, array in arrays.items()})
        refs: dict[str, tuple[BoundID[Array[Any]], ArrayType]] = {}
        for name, given in inputs.items():
            ref = given if isinstance(given, BoundID) else await self._entities.store(given)
            refs[name] = (ref, arrays[name].type)
        job_id = JobID(uuid.uuid4())
        if tx is not None:
            await self._entities.flush(tx)
            await ledger.create_job(tx, job_id, workflow.name, root, refs)
        else:
            async with self._database.transaction() as tx:
                await self._entities.flush(tx)
                await ledger.create_job(tx, job_id, workflow.name, root, refs)
        return job_id

    async def list(self) -> list[JobRecord]:
        """Every job, newest first."""
        async with self._database.transaction(snapshot=True) as tx:
            return await ledger.list_jobs(tx)

    async def status(self, job_id: JobID) -> JobReport:
        """The job's derived state.

        Raises:
            NotFoundError: If there is no such job.
        """
        # One snapshot: the invocation rows and the attempts must agree, or a claim committing
        # between two reads shows an invocation in an impossible combination.
        async with self._database.transaction(snapshot=True) as tx:
            job = await ledger.get_job(tx, job_id)
            job_attempts = await execution.list_job_attempts(tx, job_id)
            invocations = await ledger.list_invocations(tx, job_id)
            statuses = {
                status.pathname: status
                for status in (await self._invocation_statuses(tx, job_id, invocations)).values()
            }
            steps = {
                record.cache_key: _step_display(record, step_name)
                for record, step_name in await StatusQueries([job_id]).named_step_records(tx)
            }
        root = next((i for i in invocations if i.pathname == job.root), None)
        if root is not None and root.finished:
            state: JobState = "succeeded"
        elif any(s.state in ("queued", "running") for s in statuses.values()):
            # A workflow attempt is live: running, or queued under an active job attempt (under a
            # cancelled one nothing is due, so its invocations show as stopped). A step running
            # with no workflow attempt of the job waiting on it does not keep the job running: its
            # result becomes a fact, but nothing of the job will use it until a resume.
            state = "running"
        else:
            state = "stopped"
        return JobReport(
            job=job,
            state=state,
            job_attempts=job_attempts,
            invocations=statuses,
            steps=steps,
            result=None if root is None else root.result,
        )

    async def wait(self, job_id: JobID, *, poll_interval: float = 0.5) -> JobReport:
        """Wait until the job is no longer running and return its status."""
        while True:
            status = await self.status(job_id)
            if status.state != "running":
                return status
            await self._database.wait_for(job_topic(job_id), poll_interval)

    async def result(self, job_id: JobID) -> Array[Any]:
        """The root workflow's result array.

        Raises:
            JobNotFinishedError: If the job has not succeeded.
        """
        status = await self.status(job_id)
        if status.result is None:
            raise JobNotFinishedError(f"Job {job_id} is {status.state}")
        return await self._entities.load(status.result)

    async def describe(self, job_id: JobID) -> JobDescription:
        """Everything recorded about a job, as of one snapshot."""
        async with self._database.transaction(snapshot=True) as tx:
            job = await ledger.get_job(tx, job_id)
            job_attempts = await execution.list_job_attempts(tx, job_id)
            described: dict[str, InvocationDescription] = {}
            for invocation in await ledger.list_invocations(tx, job_id):
                attempts = [
                    AttemptDescription(
                        attempt,
                        await ledger.list_events(tx, attempt.id),
                        await list_logs(tx, attempt.id),
                        await provenance(
                            tx, self._entities, attempt.owner_job_attempt_id, job_attempts
                        ),
                    )
                    for attempt in await execution.list_attempts(tx, invocation.id)
                ]
                described[invocation.pathname] = InvocationDescription(
                    invocation=invocation,
                    nodes=await ledger.load_nodes(tx, invocation.id),
                    spawns=await ledger.load_spawns(tx, invocation.id),
                    attempts=attempts,
                    result_provenance=await result_provenance(
                        tx, self._entities, invocation, job_attempts
                    ),
                )
            steps = {
                d.request.cache_key: d
                for d in await self._describe_steps(tx, job_id, job_attempts=job_attempts)
            }
        return JobDescription(
            job=job, job_attempts=job_attempts, invocations=described, steps=steps
        )

    async def _describe_steps(
        self,
        tx: Transaction,
        job_id: JobID,
        requests: Sequence[JobStepRequestRecord] | None = None,
        job_attempts: Sequence[JobAttemptRecord] = (),
    ) -> list[StepDescription]:
        """Describe requested steps, in their order: each one's binding, what the policy says the
        key is to the job, and what is behind it (see :class:`StepDescription`), with the
        attempt's logs. Their status is derived together in one query, and each kind of row behind
        them is read once for all of them, so a run node's many thousands of steps cost a few
        queries, not a few per step. ``job_attempts`` are the job's, if already read (see
        :func:`provenance`).

        What is behind a step: a step result (the bound one, else the one the cache entry offers)
        when there is one, with its producing attempt if this database ran it and its producer as
        the ledger records it; otherwise the cache entry's current attempt and its owner."""
        if requests is None:
            requests = await ledger.list_job_step_requests(tx, job_id)
        if not requests:
            return []
        keys = [request.cache_key for request in requests]
        states = await StatusQueries([job_id]).step_records(tx, keys)
        specs = await ledger.get_step_requests(tx, {request.request for request in requests})
        bindings = await ledger.get_step_bindings(tx, job_id, keys)
        entries = await execution.get_cache_entries(tx, set(keys) - bindings.keys())
        step_result_ids: dict[str, StepResultID] = {}
        for key in keys:
            if (binding := bindings.get(key)) is not None:
                step_result_ids[key] = binding.step_result_id
            elif (entry := entries.get(key)) is not None and entry.step_result_id is not None:
                step_result_ids[key] = entry.step_result_id
        step_results = await ledger.get_step_results(tx, set(step_result_ids.values()))
        producing = await execution.find_attempts(
            tx, {ledger.producing_attempt(s) for s in step_result_ids.values()}
        )
        current = await execution.get_attempts(
            tx,
            {
                entry.attempt_id
                for key, entry in entries.items()
                if key not in step_result_ids and entry.attempt_id is not None
            },
        )
        behind: dict[str, tuple[AttemptRecord | None, JobAttemptID]] = {}
        for key in keys:
            if (step_result_id := step_result_ids.get(key)) is not None:
                behind[key] = (
                    producing.get(ledger.producing_attempt(step_result_id)),
                    step_results[step_result_id].job_attempt_id,
                )
            elif (entry := entries.get(key)) is not None and entry.attempt_id is not None:
                attempt = current[entry.attempt_id]
                behind[key] = (attempt, attempt.owner_job_attempt_id)
        produced = await provenances(
            tx, self._entities, {producer for _, producer in behind.values()}, job_attempts
        )
        logs = await list_logs_many(
            tx, {attempt.id for attempt, _ in behind.values() if attempt is not None}
        )
        descriptions = []
        for request in requests:
            key = request.cache_key
            spec = specs[request.request]
            attempt, producer = behind.get(key, (None, None))
            descriptions.append(
                StepDescription(
                    request,
                    spec,
                    bindings.get(key),
                    execution.step_target_status(key, states[key]),
                    _step_display(states[key], spec.step_name),
                    attempt,
                    [] if attempt is None else logs[attempt.id],
                    None if producer is None else produced[producer],
                )
            )
        return descriptions

    async def _invocation_statuses(
        self, tx: Transaction, job_id: JobID, invocations: Sequence[InvocationRecord]
    ) -> dict[InvocationID, InvocationStatus]:
        """Derive target states and their answering attempt facts together."""
        if not invocations:
            return {}
        projection = StatusQueries(
            [job_id], invocation_id=invocations[0].id if len(invocations) == 1 else None
        )
        rows = await projection.invocation_records(tx, [inv.id for inv in invocations])
        return {
            InvocationID(row.id): InvocationStatus(
                row.pathname,
                row.workflow_name,
                row.display_state,
                attempt_id=None
                if row.status_attempt_id is None
                else AttemptID(str(row.status_attempt_id)),
                error=AttemptError(**row.error)
                if row.display_state == "failed" and row.error
                else None,
                reason=row.reason if row.display_state == "interrupted" else None,
            )
            for row in rows
        }

    async def describe_invocation(self, job_id: JobID, pathname: str) -> InvocationDetail | None:
        """Everything recorded about one invocation and its derived status, as of one snapshot, or
        ``None`` if the job has no invocation at that pathname."""
        async with self._database.transaction(snapshot=True) as tx:
            try:
                invocation = await ledger.find_invocation(tx, job_id, pathname)
            except ledger.NotFoundError:
                return None
            job_attempts = await execution.list_job_attempts(tx, job_id)
            attempts = [
                AttemptDescription(
                    attempt,
                    await ledger.list_events(tx, attempt.id),
                    await list_logs(tx, attempt.id),
                    await provenance(
                        tx, self._entities, attempt.owner_job_attempt_id, job_attempts
                    ),
                )
                for attempt in await execution.list_attempts(tx, invocation.id)
            ]
            spawns = await ledger.load_spawns(tx, invocation.id)
            description = InvocationDescription(
                invocation=invocation,
                nodes=await ledger.load_nodes(tx, invocation.id),
                spawns=spawns,
                attempts=attempts,
                result_provenance=await result_provenance(
                    tx, self._entities, invocation, job_attempts
                ),
            )
            # Resolve each spawn's target, result, and state: child invocations from their records,
            # steps from the job's request at the key.
            child_ids = {s.child_invocation_id for s in spawns if s.child_invocation_id is not None}
            child_records = await ledger.get_invocations(tx, child_ids)
            statuses = await self._invocation_statuses(
                tx, job_id, [invocation, *child_records.values()]
            )
            status = statuses[invocation.id]
            child_states: dict[InvocationID, InvocationState] = {
                cid: statuses[cid].state for cid in child_records
            }
            step_keys = {s.step_cache_key for s in spawns if s.step_cache_key is not None}
            requests = await ledger.get_job_step_requests(tx, job_id, step_keys)
            unrequested = step_keys - requests.keys()
            assert not unrequested, f"spawned steps at {sorted(unrequested)!r} were not requested"
            steps = {
                d.request.cache_key: d
                for d in await self._describe_steps(
                    tx, job_id, list(requests.values()), job_attempts
                )
            }
        return InvocationDetail(
            description,
            status,
            child_pathnames={cid: r.pathname for cid, r in child_records.items()},
            child_results={cid: r.result for cid, r in child_records.items()},
            step_results={
                key: None if d.binding is None else d.binding.result for key, d in steps.items()
            },
            child_states=child_states,
            step_states={key: d.display.state for key, d in steps.items()},
        )

    async def describe_step(self, job_id: JobID, cache_key: str) -> StepDetail | None:
        """One step the job requested and its derived state, as of one snapshot, or ``None`` if the
        job made no request at that key."""
        async with self._database.transaction(snapshot=True) as tx:
            request = await ledger.get_job_step_request(tx, job_id, cache_key)
            if request is None:
                return None
            (description,) = await self._describe_steps(tx, job_id, [request])
            status = description.display
        return StepDetail(description, status)

    async def cancel(
        self, job_id: JobID, *, hard: bool = False, reason: str = "cancelled"
    ) -> JobAttemptRecord:
        """Cancel the job's active job attempt (:func:`~fxtr.runner.postgres.execution.cancel`):
        the owning process starts nothing more. Soft (the default) lets running attempts finish on
        their own terms; hard evicts the job attempt, abandoning them. Resume to continue.

        Returns:
            The job attempt, now cancelled.

        Raises:
            NotFoundError: If there is no such job.
            NotCancellableError: If the job has no active job attempt.
        """
        async with self._database.transaction() as tx:
            await ledger.get_job(tx, job_id)
            job_attempt = await execution.active_job_attempt(tx, job_id)
            if job_attempt is None:
                raise execution.NotCancellableError(f"Job {job_id} has no active job attempt")
            await execution.cancel(tx, job_attempt, hard=hard, reason=reason)
            return await execution.get_job_attempt(tx, job_attempt.id)

    async def resume(
        self,
        job_id: JobID,
        *,
        owner: str,
        code_version: BoundID[CodeVersion],
        overwrite_cache_conflicts: bool = False,
    ) -> JobAttemptRecord:
        """Take the job for ``owner``, asking for everything unfinished in it to be finished
        (:func:`~fxtr.runner.postgres.execution.resume`): a new job attempt starts, recording
        ``code_version``, once the latest one is settled or stale (a stale one is taken over).
        Running what is then due is the job runner's business (:mod:`fxtr.runner.postgres.runner`).

        Args:
            job_id: The job.
            owner: The process that will run the new job attempt.
            code_version: The code that process runs.
            overwrite_cache_conflicts: If ``True``, the new job attempt overwrites cache
                conflicts with completed results.

        Returns:
            The job attempt now holding the job, owned by ``owner``.

        Raises:
            NotFoundError: If there is no such job.
            NotResumableError: If the job has succeeded.
            JobRunningElsewhereError: If another process holds a fresh lease on the job.
        """
        async with self._database.transaction() as tx:
            resumed = await execution.resume(
                tx,
                job_id,
                owner=owner,
                code_version=code_version,
                overwrite_cache_conflicts=overwrite_cache_conflicts,
            )
        if isinstance(resumed, execution.AlreadyFinished):
            raise execution.NotResumableError(f"Job {job_id} has succeeded")
        return resumed


class JobNotFinishedError(ledger.LedgerError):
    """The job has no result yet."""


def _step_display(record: StepStateRecord, step_name: str) -> StepStatus:
    """A step's displayed status from its row of the step relation: the state and answering attempt
    the relation derives for display, the error the policy reports for a failed step, and what
    owns the key for a step in conflict."""
    judged = execution.step_target_status(record.cache_key, record)
    return StepStatus(
        record.cache_key,
        step_name,
        record.display_state,
        record.display_attempt_id,
        judged.error if isinstance(judged, execution.Stopped) else None,
        _cache_conflict(record),
    )


async def provenance(
    tx: Transaction,
    entities: PostgresEntityStorage,
    job_attempt_id: JobAttemptID,
    job_attempts: Sequence[JobAttemptRecord] = (),
) -> Provenance:
    """The code a job attempt's process ran: its ledger fact, with the code version loaded (for an
    attempt, its owner's). ``job_attempts`` are ones already read (the job's own, typically); the
    fact is read from the ledger otherwise, as for a step result another job produced, whose job
    this database may not have."""
    return (await provenances(tx, entities, [job_attempt_id], job_attempts))[job_attempt_id]


async def provenances(
    tx: Transaction,
    entities: PostgresEntityStorage,
    job_attempt_ids: Collection[JobAttemptID],
    job_attempts: Sequence[JobAttemptRecord] = (),
) -> dict[JobAttemptID, Provenance]:
    """:func:`provenance` for many job attempts, by id: the facts not among ``job_attempts`` read
    in one query, and each distinct code version loaded once (many steps share a few)."""
    known = {ja.id: ja for ja in job_attempts}
    facts = {
        ja.id: JobAttemptProvenanceRecord(
            id=ja.id, job_id=ja.job_id, code_version=ja.code_version, created_at=ja.created_at
        )
        for job_attempt_id in job_attempt_ids
        if (ja := known.get(job_attempt_id)) is not None
    }
    facts |= await ledger.get_job_attempt_provenances(tx, set(job_attempt_ids) - facts.keys())
    versions = {fact.code_version.entity_id: fact.code_version for fact in facts.values()}
    loaded = await entities.load_raw_data_many(list(versions))
    code_versions = {
        entity_id: versions[entity_id].loader.convert(data)
        for entity_id, data in zip(versions, loaded, strict=True)
    }
    return {
        job_attempt_id: Provenance(fact, code_versions[fact.code_version.entity_id])
        for job_attempt_id, fact in facts.items()
    }


async def result_provenance(
    tx: Transaction,
    entities: PostgresEntityStorage,
    invocation: InvocationRecord,
    job_attempts: Sequence[JobAttemptRecord] = (),
) -> Provenance | None:
    """The code that produced an invocation's result, as the ledger records it with the result;
    ``None`` while it is unfinished."""
    if invocation.job_attempt_id is None:
        return None
    return await provenance(tx, entities, invocation.job_attempt_id, job_attempts)


def _cache_conflict(projection: StepStateRecord) -> CacheConflict | None:
    """Get the cache conflict of one requested step.

    Args:
        projection: The row of the step relation for the step.

    Returns:
        The conflict, if the step relation puts the step in ``conflict``. Its kind is
        ``completed`` if the entry offers a step result, and ``running`` if not. ``None`` if the
        step is in a different state.
    """
    if projection.target_state != "conflict":
        return None
    assert projection.entry_step_name is not None
    kind = "completed" if projection.offered_step_result_id is not None else "running"
    return CacheConflict(projection.cache_key, projection.entry_step_name, kind)
