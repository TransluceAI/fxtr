"""The job runner: one process running one job.

**What a runner is.** A job is run in episodes; each episode is a *job attempt*, owned by the
process that runs it. A :class:`JobRunner` is that process's side of one job attempt: it holds
the job attempt's lease by heartbeating, decides *when* each unit of due work starts and how many
run at once, runs every attempt's body in this process through a
:data:`~fxtr.runner.driver.RunExecutor`, and stops when the job attempt is settled. It decides
nothing about *what* is due or whether starting it is allowed: that is the execution policy in
:mod:`fxtr.runner.postgres.execution`, which this module is written against (read its module
docstring first; the terms below are defined there).

**Where work comes from.** The database holds no queue. What the job attempt is due to start is
derivable (:func:`~fxtr.runner.postgres.execution.pending_work`), and the runner keeps an
in-process queue of work items, :class:`~fxtr.runner.postgres.execution.StartInvocation` and
:class:`~fxtr.runner.postgres.execution.StartStep`, fed four ways. At startup the runner *seeds*
the queue from ``pending_work``. While it runs, the evaluator of each attempt *pushes* the work it
creates, the children and runnable step requests of an expansion, through the :class:`WorkSink`
this runner is to it, from the creating transaction's commit callback, so work is queued where its
ledger fact is written. An evaluator that finds a target due with nothing running it, which
happens when another job's attempt at a shared cache key ended without a result, pushes that too.
And the runner *reconciles*: it re-derives everything due whenever it has nothing live and on an
interval, as a safety net for anything the other three missed. The queue is an ordered
set: an item already queued or running is not queued again, and items start in the order they
arrived; starting an item is idempotent in the database (``start_attempt`` inserts nothing when the
invocation is already running or the step resolved otherwise), so a stale or duplicate item costs
one refused
transaction and nothing else.

**How work runs.** :meth:`JobRunner.run` opens one task group holding the heartbeat task and one
*item task* per work item, with the scheduling loop in the runner's own task. An item task first
takes the step capacity limiter if the item is a step (steps are the expensive resource; workflow
bodies are unbounded, since they are coordination that mostly waits and bounding them could stall
a job), then starts the attempt in one transaction, then *is* the attempt task: it builds the live
attempt, runs its body and helper tasks in one task group, waits for the attempt to have ended,
and finally makes sure its outcome is in the ledger (see
:mod:`fxtr.runner.postgres.attempts` for the end-and-close lifecycle: the task is the
closer of last resort, under a shield with a deadline, so an attempt's row never stays
``running`` after its task ends, whatever cancelled it). The item counts as *live* from the moment
its task is spawned until the task ends, so the loop never mistakes a task waiting for the limiter
or in its start transaction for nothing.

**Settling.** A job attempt is *settled* when it leaves ``active`` for ``succeeded`` (the root
invocation has its result), ``stopped`` (nothing of the job is running and nothing is due) or
``cancelled`` (a person stopped it), and a runner runs exactly as long as its job attempt is
active, then finishes what it has live and exits. Settling ``succeeded`` or ``stopped`` is this
runner's job, through :func:`~fxtr.runner.postgres.execution.settle`, which owns the rule and, when
it does not settle, hands back what is due: settling and reconciling are one lock-first pass over
the durable state. The loop makes that pass whenever it has nothing live and on the reconciliation
interval, so an episode that starts nothing at all (every item refused) still settles, and a job
whose last item ends settles at once. The loop does not make the pass as each item ends, and
closing an attempt does not settle: the pass reads the whole job while holding the job attempt's
row, which every start and ending waits for, so a pass per ending would make a job of N steps cost
N passes over N requests.

**Stopping.** Four things end a run, and they differ in who settled the job attempt and whether
this runner may still finish what it has live:

* *The work finished.* The loop settled the job attempt ``succeeded`` or ``stopped``; nothing is
  live; :meth:`JobRunner.run` returns it.
* *Interrupted here* (what a Ctrl-C handler calls, :meth:`JobRunner.interrupt`). The first call
  settles the job attempt ``cancelled`` and *drains*: nothing more starts, the heartbeat continues,
  and live item tasks finish (a step in flight completes and its result is on the cache entry for
  the job to take when it is next run). The second call cancels the item tasks, whose attempts are
  then abandoned. Cancelling the runner's task from outside is the abrupt form of the second call,
  and the shielded ``finally`` of :meth:`JobRunner.run` still settles the job attempt ``cancelled``
  if nothing else did, so a later resume needs no takeover.
* *Cancelled elsewhere.* The heartbeat reports a settled state: another process soft-cancelled the
  job (:func:`~fxtr.runner.postgres.execution.cancel`). The runner drains as for the first
  interrupt.
* *Evicted.* The heartbeat's conditional update finds no row, or the reconcile pass reads the job
  attempt as evicted: a takeover by a resume elsewhere, or a hard cancel, abandoned everything this
  runner owned and settled the job attempt. The runner cancels its tasks (each attempt's teardown
  finds its row already abandoned and closes locally), settles nothing, and raises
  :class:`JobEvictedError`.

The heartbeat is this runner's main channel for the last two: it refreshes the lease so a human or
a resume can tell the process is alive, and its return value is how the runner learns it was
cancelled or evicted. The runner never judges anyone else's lease.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import socket
import uuid
from datetime import timedelta
from typing import Protocol

import anyio
import anyio.abc

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityNotFoundError, InvalidEntityDataError
from fxtr.entity_defns.array import ArrayError
from fxtr.entity_defns.dataclass_bases import ConversionError
from fxtr.experiment.registry import GLOBAL_REGISTRY, FunctionRegistry
from fxtr.project.code_version import CodeVersion
from fxtr.runner.assembly import StepRun
from fxtr.runner.driver import RunExecutor, drive_workflow_attempt, in_process_executor
from fxtr.runner.postgres import execution, ledger
from fxtr.runner.postgres.attempts import (
    Abandoned,
    Attempt,
    Completed,
    Failed,
    Interrupted,
    Outcome,
    StepAttempt,
)
from fxtr.runner.postgres.database import Database
from fxtr.runner.postgres.entities import PostgresEntityStorage
from fxtr.runner.postgres.evaluator import Dag, Evaluator, NodeState
from fxtr.runner.postgres.execution import (
    JobAttemptRecord,
    StaleAttemptError,
    Started,
    StartedStep,
    StartStep,
    WorkItem,
)
from fxtr.runner.postgres.ledger import JobID
from fxtr.runner.postgres.recorder import Recorder
from fxtr.runner.session import (
    AttemptError,
    AttemptID,
    RunnerError,
    StepAssignment,
)
from fxtr.runner.steps import StepRequest

logger = logging.getLogger(__name__)

ATTEMPT_TEARDOWN_SECONDS = 30.0
"""How long an item task's ``finally`` may spend closing its attempt's outcome, and the runner's
own ``finally`` settling its job attempt, before giving up (what is left running is then a
takeover's to sweep)."""

UNREADABLE_REQUEST = (
    EntityNotFoundError,  # storage does not have the entity the ledger names
    InvalidEntityDataError,  # what it has is not a tagged entity
    ConversionError,  # the conversion layer's boundary: it is not a StepRequest
    ArrayError,  # its inputs are not the arrays a step can be called with
    ledger.LedgerError,  # the ledger disagrees with itself about the call
)
"""Reading a step's recorded request produced something it cannot be called from.

Every one of these says the record is wrong, not that reading it went wrong, so retrying reads
the same bytes to the same end: the runner fails the attempt instead of abandoning the row for
another try. Anything else — a database that faltered, most of all — is left to propagate, so
that row is abandoned and reconciliation picks it up again.
"""


class JobEvictedError(RunnerError):
    """The runner lost the job: its job attempt was evicted (a takeover or a hard cancel), so
    nothing it still holds may write. Whatever it was running has been abandoned by the evictor."""


@dataclasses.dataclass(frozen=True)
class RunLimits:
    """How much of a job runs at once.

    Attributes:
        steps: Step attempts running concurrently. Workflow bodies are not limited: they are
            coordination, cheap and mostly waiting, and bounding them could stall a job whose
            running bodies all wait on work that is not started. Every body runs on this
            process's event loop, so the limit pays off for steps that await (a model's API, say)
            and does nothing for CPU-bound ones.
    """

    steps: int = 32


@dataclasses.dataclass(frozen=True)
class RunnerSettings:
    """The runner's clocks, in seconds.

    Attributes:
        heartbeat_interval: Between refreshes of the lease.
        reconcile_interval: How long the scheduling loop lets pass without re-deriving what is
            due; it also re-derives whenever nothing is live.
        poll_interval: The evaluators' fallback wait for a change made in another process.
    """

    heartbeat_interval: float = 5.0
    reconcile_interval: float = 10.0
    poll_interval: float = 0.5


class WorkSink(Protocol):
    """Where a transaction that created work pushes it: the runner's queue."""

    def enqueue(self, item: WorkItem) -> None:
        """Queue one unit of work, if it is not queued or running already."""
        ...


def default_owner() -> str:
    """An owner string for this process: its host, its pid, and a random token, so two runs of one
    process are told apart."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class JobRunner:
    """One process's episode of running a job (see the module docstring).

    Args:
        database: The runner's database.
        job_attempt: The job attempt this runner owns, as :func:`run_job` (or
            :func:`~fxtr.runner.postgres.execution.resume`) created it. Its ``owner`` is what the
            heartbeat refreshes the lease under.
        execute: How to execute one run of a body.
        limits: How much runs at once.
        settings: The clocks.
        verify_determinism: Whether every workflow attempt's return is held until a verification
            run has executed the body again and done the same things (see
            :mod:`fxtr.runner.assembly`).
    """

    def __init__(
        self,
        database: Database,
        job_attempt: JobAttemptRecord,
        *,
        execute: RunExecutor,
        limits: RunLimits = RunLimits(),
        settings: RunnerSettings = RunnerSettings(),
        verify_determinism: bool = True,
    ) -> None:
        self._database = database
        self._job_attempt = job_attempt
        self._execute = execute
        self._settings = settings
        self._verify_determinism = verify_determinism
        self._steps = anyio.CapacityLimiter(limits.steps)
        self._queue: dict[WorkItem, None] = {}  # an ordered set: items start in arrival order
        self._live: set[WorkItem] = set()
        self._item_scopes: dict[WorkItem, anyio.CancelScope] = {}
        self._wake = anyio.Event()
        self._tg: anyio.abc.TaskGroup | None = None
        self._next_reconcile = anyio.current_time()  # due at once: the first pass is the seed
        self._interrupts = 0
        self._stopping = False
        self._evicted = False
        self._settled: JobAttemptRecord | None = None  # the job attempt as the run ended

    @property
    def job_attempt(self) -> JobAttemptRecord:
        """The job attempt this runner owns, as it was when the runner took it."""
        return self._job_attempt

    @property
    def job_id(self) -> JobID:
        return self._job_attempt.job_id

    # ---- the queue ----

    def enqueue(self, item: WorkItem) -> None:
        """Queue one unit of work (:class:`WorkSink`). Idempotent: an item already queued or
        running under this runner is ignored, and so is anything arriving while it drains."""
        if self._stopping or item in self._queue or item in self._live:
            return
        self._queue[item] = None
        self._signal()

    def interrupt(self) -> None:
        """Ask the runner to stop, as a Ctrl-C does. The first call settles the job attempt
        ``cancelled`` and drains: nothing more is started, and the item tasks already running are
        left to finish. A second call stops them too, abandoning their attempts. Idempotent
        afterwards. Call it on the event loop's thread (from a handler installed with
        ``anyio.open_signal_receiver`` or the loop's own signal handling, not a raw
        ``signal.signal`` handler): it touches the loop's primitives."""
        self._interrupts += 1
        if self._interrupts > 1:
            for scope in list(self._item_scopes.values()):
                scope.cancel()
        self._signal()

    def _signal(self) -> None:
        """Wake the scheduling loop: something it can act on changed."""
        self._wake.set()

    # ---- running ----

    async def run(self) -> JobAttemptRecord:
        """Run the job until its job attempt settles, and return it as settled.

        Returns:
            The job attempt, settled ``succeeded``, ``stopped``, or ``cancelled``.

        Raises:
            JobEvictedError: If the lease was taken away while the runner held it.
        """
        try:
            async with anyio.create_task_group() as tg:
                self._tg = tg
                tg.start_soon(self._heartbeat)
                self._settled = await self._schedule()
                tg.cancel_scope.cancel()  # the heartbeat is the only task left
        finally:
            self._tg = None
            if self._settled is None and not self._evicted:
                # Stopped without settling: cancelled from outside, or the loop raised. Leave the
                # job attempt settled all the same, so a later resume needs no takeover. A failure
                # here must not hide what stopped the run: a takeover settles it later anyway.
                with anyio.move_on_after(ATTEMPT_TEARDOWN_SECONDS, shield=True):
                    try:
                        await self._cancel_self("the runner stopped")
                    except Exception:
                        logger.exception("could not settle job attempt %s", self._job_attempt.id)
        if self._settled is None:
            # Raised out here rather than in the loop, so the caller sees the error itself and not
            # the task group's group.
            raise JobEvictedError(await self._eviction_reason())
        return self._settled

    async def _schedule(self) -> JobAttemptRecord | None:
        """The scheduling loop: keep the queue moving, settle when there is nothing left, and
        return the settled job attempt, or ``None`` if the lease was taken away. Runs in the
        runner's own task; the item tasks it starts run beside it."""
        while True:
            if self._evicted:
                return None
            if self._interrupts > 0 and not self._stopping:
                await self._cancel_self("interrupted")
            if not self._live or anyio.current_time() >= self._next_reconcile:
                settled = await self._reconcile()
                if self._evicted:
                    return None
                if settled is not None and not self._live:
                    return settled
            if not self._stopping:
                self._start_queued()
            await self._wait(until=self._next_reconcile)

    async def _wait(self, *, until: float) -> None:
        """Wait until something the loop acts on may have changed (an enqueue, an item task
        ending, the heartbeat, an interrupt), or until the clock reaches ``until``."""
        wake = self._wake
        if not wake.is_set():
            with anyio.move_on_after(max(0.0, until - anyio.current_time())):
                await wake.wait()
        self._wake = anyio.Event()

    async def _reconcile(self) -> JobAttemptRecord | None:
        """One pass of the policy over the durable state, in one transaction that takes the job
        attempt's row first (:func:`~fxtr.runner.postgres.execution.settle`): settle the job
        attempt if the job is finished or nothing is left, else enqueue everything due. The first
        pass is the seed. Schedules the next interval pass.

        Returns:
            The job attempt once it is no longer active, whichever way that happened: this pass
            settled it, or another process cancelled it. ``None`` while the episode is still
            going, and when the job attempt was evicted (the pass may see that before the
            heartbeat does; it is the same eviction, and the run ends the same way).
        """
        self._next_reconcile = anyio.current_time() + self._settings.reconcile_interval
        async with self._database.transaction() as tx:
            found = await execution.settle(tx, self.job_id)
            current = await execution.get_job_attempt(tx, self._job_attempt.id)
        if current.evicted:
            self._evicted = True
            return None
        for item in found.due:
            self.enqueue(item)  # ignored while draining
        return None if current.active else current

    def _start_queued(self) -> None:
        """Start an item task per queued item. The item counts as live from here, before its task
        has run at all, so the settle check cannot mistake a task waiting for the limiter for
        nothing."""
        assert self._tg is not None
        while self._queue:
            item = next(iter(self._queue))
            del self._queue[item]
            self._live.add(item)
            self._tg.start_soon(self._run_item, item)

    async def _cancel_self(self, reason: str) -> None:
        """Settle the job attempt ``cancelled`` and drain: start nothing more, keep heartbeating,
        let the live item tasks finish. A no-op on a job attempt that has settled already (someone
        else cancelled it, or it succeeded)."""
        self._stopping = True
        self._queue.clear()
        async with self._database.transaction() as tx:
            job_attempt = await execution.get_job_attempt(tx, self._job_attempt.id)
            if job_attempt.active:
                await execution.cancel(tx, job_attempt, hard=False, reason=reason)

    async def _eviction_reason(self) -> str:
        async with self._database.transaction() as tx:
            job_attempt = await execution.get_job_attempt(tx, self._job_attempt.id)
        return (
            f"Job attempt {job_attempt.id} of job {job_attempt.job_id} was evicted:"
            f" {job_attempt.evicted_reason}"
        )

    # ---- the heartbeat ----

    async def _heartbeat(self) -> None:
        """Refresh the lease on its interval until the runner stops. A refresh that finds the job
        attempt evicted ends the run (:class:`JobEvictedError`); one that reports a settled state
        is a cancel from another process, and the runner drains. A draining runner keeps
        heartbeating, so a resume elsewhere sees it alive. A refresh that fails is logged and
        retried next interval; the heartbeat never ends the run on its own."""
        while True:
            await anyio.sleep(self._settings.heartbeat_interval)
            try:
                async with self._database.transaction() as tx:
                    state = await execution.heartbeat(
                        tx, self._job_attempt.id, self._job_attempt.owner
                    )
            except Exception:
                # A missed heartbeat costs nothing by itself (staleness only informs); try again
                # next interval. A database that is really gone fails the loop's own transactions.
                logger.exception("heartbeat of job attempt %s failed", self._job_attempt.id)
                continue
            if state is None:
                self._evicted = True
                self._signal()
                return
            if state != "active":
                self._stopping = True
                self._queue.clear()
                self._signal()

    # ---- item tasks ----

    async def _run_item(self, item: WorkItem) -> None:
        """One item, from the queue to a closed attempt: take the step limiter, start the attempt,
        and run it. Ends quietly when there was nothing to start after all."""
        scope = anyio.CancelScope()
        self._item_scopes[item] = scope
        try:
            with scope:
                if isinstance(item, StartStep):
                    async with self._steps:
                        await self._start_and_run(item)
                else:
                    await self._start_and_run(item)
        finally:
            self._item_scopes.pop(item, None)
            self._live.discard(item)
            self._signal()

    async def _start_and_run(self, item: WorkItem) -> None:
        async with self._database.transaction() as tx:
            started = await execution.start_attempt(tx, self._job_attempt, item)
        if started is None:
            return  # something else is live, it finished meanwhile, or the lease is gone
        if isinstance(started, Started):
            await self._run_workflow_attempt(started)
        else:
            await self._run_step_attempt(started)

    async def _run_workflow_attempt(self, started: Started) -> None:
        """Run one workflow attempt, from a freshly inserted ``running`` row to its outcome in the
        ledger.

        A workflow attempt is three cooperating tasks over one host-side
        :class:`~fxtr.runner.postgres.attempts.Attempt`: the *body* (the user's workflow function,
        run through the executor and the shared driver, which with verification on runs it a second
        time to verify its return), the *recorder* (commits what the body describes), and the
        *evaluator* (computes node values, expands run nodes into children and step requests,
        collects their results, and ends the attempt when its work is done). This
        task builds the attempt, seeding its view of the invocation's DAG from what the ledger
        already holds (a resumed invocation has nodes and values from earlier attempts; derived
        values and errors are recomputed by the evaluator's first pass), runs the other two in a
        task group with the body, and waits for the attempt to have ended, by the evaluator or by
        a failure anywhere. The body runs inside a cancel scope the attempt owns (``cancel_body``),
        so the host can stop it, on a determinism violation, say.

        Whatever ends the task group, the ``finally`` makes the outcome durable: it ends the
        attempt ``abandoned`` if it had not ended (the task was cancelled by an interrupt or an
        eviction) and closes its outcome if the ledger does not have it yet (a helper's own write
        was cancelled in flight), under a shield with a deadline. Nothing is flushed there: the
        recorder was a task of the group and has ended, and what it had committed is on record.
        """
        # Built before the first await: from here on the ``finally`` covers the running row.
        entities = PostgresEntityStorage(self._database)
        dag: Dag = {}
        body_scope = anyio.CancelScope()
        attempt = Attempt.create(
            started.attempt_id,
            started.invocation,
            dag,
            inputs=started.inputs,
            spawns=started.spawns,
            entities=entities,
            database=self._database,
            cancel_body=body_scope.cancel,
            on_change=self._attempt_changed,
        )
        logger.info("job %s: workflow %s started", _short(self.job_id), attempt.pathname)
        try:
            for record in started.nodes:
                value = None if record.value is None else await entities.load(record.value)
                dag[record.identity] = NodeState(
                    record.node, record.type, value=value, stored=record.value
                )
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(Recorder(attempt, self._database).run)
                tasks.start_soon(
                    Evaluator(attempt, self._database, self._settings.poll_interval, self).run
                )
                with body_scope:
                    await drive_workflow_attempt(
                        attempt,
                        self._execute,
                        run_id=attempt.id,
                        verify_determinism=self._verify_determinism,
                    )
                await attempt.wait_ended()
        finally:
            with anyio.CancelScope(shield=True), anyio.move_on_after(ATTEMPT_TEARDOWN_SECONDS):
                attempt.abandon("the runner stopped")
                await attempt.close()
            _log_end(_short(self.job_id), f"workflow {attempt.pathname}", attempt.outcome)

    async def _run_step_attempt(self, started: StartedStep) -> None:
        """Run one step attempt, from a freshly inserted ``running`` row to its outcome in the
        ledger.

        A step attempt is simpler than a workflow's: one run of the step function over the inputs
        in the request entity (loaded here), with a background task that flushes its ``ctx.log``
        lines. The run completes or fails the host-side
        :class:`~fxtr.runner.postgres.attempts.StepAttempt`; this task waits for it to end, and
        its ``finally`` is the same as a workflow attempt's: end it ``abandoned`` if it had not
        ended, close if the ledger does not have it yet, under a shield with a deadline.
        """
        record = started.attempt
        assert record.cache_key is not None and record.request is not None
        entities = PostgresEntityStorage(self._database)
        attempt: StepAttempt | None = None
        try:
            # The running row exists from here; until the attempt object does, the ``finally``
            # abandons the row directly.
            logger.info(
                "job %s: step %s started (%s)%s",
                _short(self.job_id),
                record.cache_key,
                started.step_name,
                ", overwriting a different request's cached result" if started.overwrote else "",
            )
            try:
                request = await entities.load(record.request.bind_type(StepRequest))
            except UNREADABLE_REQUEST as exc:
                # The ledger's record of this call is unreadable, which no retry fixes. That is
                # this attempt's failure and nobody else's: the job's other work carries on. A
                # database that merely faltered raises something else, and the item task's
                # ``finally`` abandons the row for the runner to try again.
                await self._fail_row(record.id, AttemptError.from_exception(exc))
                return
            attempt = StepAttempt(
                id=record.id,
                cache_key=record.cache_key,
                assignment=StepAssignment(
                    attempt_id=record.id,
                    step_name=started.step_name,
                    cache_key=record.cache_key,
                    inputs=request.inputs,
                ),
                entities=entities,
                _database=self._database,
                _on_change=self._attempt_changed,
            )
            async with anyio.create_task_group() as tasks:
                attempt.start_log_flusher(tasks)
                await self._execute(StepRun(attempt.id, attempt))
                await attempt.wait_ended()
        finally:
            with anyio.CancelScope(shield=True), anyio.move_on_after(ATTEMPT_TEARDOWN_SECONDS):
                if attempt is None:
                    await self._abandon_row(record.id)
                else:
                    attempt.abandon("the runner stopped")
                    await attempt.close()
            _log_end(_short(self.job_id), f"step {record.cache_key}", attempt and attempt.outcome)

    async def _fail_row(self, attempt_id: AttemptID, error: AttemptError) -> None:
        """Close a step attempt's row as failed before the attempt object exists. Nothing to do
        if the ledger closed it already."""
        try:
            async with self._database.transaction() as tx:
                active = await execution.guard_step(tx, attempt_id, ending=True)
                await execution.close_step_attempt(tx, active, "failed", error=error)
        except StaleAttemptError:
            pass

    async def _abandon_row(self, attempt_id: AttemptID) -> None:
        """Close a step attempt's row as abandoned when there is no attempt object to close: its
        task was cancelled while reading the request, or reading it raised. Nothing to do if the
        ledger closed it already, which is what a row this task has already failed looks like."""
        try:
            async with self._database.transaction() as tx:
                active = await execution.guard_step(tx, attempt_id, ending=True)
                await execution.close_step_attempt(
                    tx, active, "abandoned", reason="the runner stopped"
                )
        except StaleAttemptError:
            pass

    def _attempt_changed(self, attempt: Attempt | StepAttempt) -> None:
        """An attempt has ended: the loop should re-check whether anything is left."""
        self._signal()


def _short(job_id: JobID) -> str:
    """A job's id in the attempt lines: its first eight hex digits, enough to tell apart the jobs
    one process runs (the client logs the full id when a job settles)."""
    return job_id.hex[:8]


def _log_end(job_id: str, what: str, outcome: Outcome | None) -> None:
    """The attempt-end line: how the attempt of ``what`` (``workflow <pathname>`` or ``step <cache
    key>``) ended, at INFO, or WARNING when it failed."""
    match outcome:
        case Completed():
            logger.info("job %s: %s completed", job_id, what)
        case Failed(error=error):
            logger.warning(
                "job %s: %s failed: %s: %s", job_id, what, error.type_name, error.message
            )
        case Interrupted(reason=reason):
            logger.info("job %s: %s interrupted: %s", job_id, what, reason)
        case Abandoned(reason=reason):
            logger.info("job %s: %s abandoned: %s", job_id, what, reason)
        case None:
            # The step's row was abandoned before its attempt object existed.
            logger.info("job %s: %s abandoned: the runner stopped", job_id, what)


async def run_job(
    database: Database,
    job_id: JobID,
    registry: FunctionRegistry = GLOBAL_REGISTRY,
    *,
    owner: str | None = None,
    limits: RunLimits = RunLimits(),
    settings: RunnerSettings = RunnerSettings(),
    verify_determinism: bool = True,
    code_version: BoundID[CodeVersion],
    take_over_after: timedelta | None = None,
    overwrite_cache_conflicts: bool = False,
) -> JobAttemptRecord:
    """Take a job and run it here until it settles: one
    :func:`~fxtr.runner.postgres.execution.resume` under ``owner`` (which creates the first job
    attempt, or the next one, taking a stale one over), then a :class:`JobRunner` over the
    in-process executor for ``registry``.

    Args:
        database: The runner's database.
        job_id: The job, as :meth:`~fxtr.runner.postgres.jobs.Jobs.create` created it.
        registry: The steps and workflows this process can run; the global registry (everything
            imported so far) by default.
        owner: The owner string to hold the lease under; defaults to :func:`default_owner`.
        limits: How much runs at once.
        settings: The clocks.
        verify_determinism: Whether returns are verified by a second run of the body.
        code_version: The code this process runs, recorded on the job attempt it creates. It must
            already be committed to the entity store; a write-behind store must be flushed first.
            It should describe ``registry``: the functions it locates are the ones that run.
        take_over_after: How long another process's lease on the job may have gone without a
            heartbeat before this one takes the job over instead of being refused.
        overwrite_cache_conflicts: If ``True``, the job attempt that this call creates overwrites
            cache conflicts with completed results (see
            :func:`~fxtr.runner.postgres.execution.resolve_step`).

    Returns:
        The job attempt, settled. For a job that has already succeeded, its last job attempt,
        settled ``succeeded`` (there is nothing to run).

    Raises:
        JobRunningElsewhereError: If another process holds a fresh lease on the job.
        JobEvictedError: If the lease was taken away while this runner held it.
    """
    async with database.transaction() as tx:
        resumed = await execution.resume(
            tx,
            job_id,
            owner=owner or default_owner(),
            code_version=code_version,
            take_over_after=take_over_after,
            overwrite_cache_conflicts=overwrite_cache_conflicts,
        )
    if isinstance(resumed, execution.AlreadyFinished):
        assert resumed.job_attempt is not None  # a result takes an attempt, which takes one
        return resumed.job_attempt
    job_attempt = resumed
    runner = JobRunner(
        database,
        job_attempt,
        execute=in_process_executor(registry),
        limits=limits,
        settings=settings,
        verify_determinism=verify_determinism,
    )
    return await runner.run()


__all__ = [
    "JobEvictedError",
    "JobRunner",
    "RunLimits",
    "RunnerSettings",
    "WorkSink",
    "default_owner",
    "run_job",
]
