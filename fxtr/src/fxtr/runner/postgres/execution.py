"""The execution state and the policy that decides what runs: who owns what, who may do what,
and how that is enforced against the database.

Where the ledger (:mod:`fxtr.runner.postgres.ledger`) records what is true about an experiment,
this module owns everything about executing it. It is the reference for the runner's ownership
model; :mod:`fxtr.runner.postgres.runner` (the process that runs a job) and
:mod:`fxtr.runner.postgres.jobs` (the commands and the derived status) are written against it.

**The pieces.** A *job* is run in *episodes*, each a **job attempt**: one process's attempt to
finish the job. A job has at most one *active* job attempt. Every call of user code is an
**attempt**: of a workflow body on behalf of an invocation, or of a step for a request at a cache
key. Every attempt is owned by exactly one job attempt, the one whose process runs it
(``owner_job_attempt_id``), and is ``running`` from the moment its row exists until it is
``completed``, ``failed``, ``interrupted``, or ``abandoned``.
Nothing is queued in the database: what a job attempt is *due* to run is derived
(:func:`pending_work`) and its process starts it (:func:`start_attempt`).

**The two axes of a job attempt.** Its *state* says whether the episode is still deciding:
``active``, then settled as ``succeeded`` (the root has its result), ``stopped`` (nothing of it
runs and nothing is due), or ``cancelled`` (a person stopped it). Its *lease* says whether its
process is trusted: the job attempt names an ``owner`` and carries a ``heartbeat_at`` that process
refreshes while it runs, and it is *fresh* while the heartbeat is recent, *stale* when it is not
(its process presumed dead), and *evicted* once someone has taken its work away. The axes are
independent, and each combination means something:

* *active, fresh*: the normal case; the owner runs the job.
* *cancelled, fresh*: the owner is draining, starting nothing more while what it has live finishes.
* *active, stale*: the owner is presumed dead; a resume of the job may take it over.
* *evicted*: its running attempts were abandoned by whoever evicted it, and it is settled (a
  takeover settles it ``stopped``, a hard cancel ``cancelled``). Its process finds out on its next
  heartbeat.

**Who may do what.**

* Only a *running* attempt writes on its own behalf, and every such transaction begins with
  :func:`guard` (or :func:`guard_step`), which checks exactly that. Eviction abandons every
  running attempt the job attempt owns, and nothing can be started under it afterwards, so an
  abandoned row is the whole of a revoked right to write.
* Only the *active* job attempt decides what is due for the job and whether an unfinished target
  is pending or stopped (:func:`pending_work`, :func:`status`, :func:`step_status`); only its
  owner starts attempts (:func:`start_attempt`).
* Anyone may *settle* a job attempt ``cancelled`` (:func:`cancel`). The owner settles it
  ``succeeded`` or ``stopped`` through :func:`settle` as its work finishes or runs out; the rule
  is a fact about the job (the root has its result; nothing runs and nothing is due), so a
  :func:`resume` applies it too, and a job attempt whose process died between its last attempt's
  close and its own settle is settled by whoever next resumes the job.
* Only a *user action* evicts: a takeover (:func:`resume` on a job whose active job attempt is
  stale), a hard :func:`cancel`, or an explicit :func:`evict`.

**Staleness informs; only eviction acts.** No decision in this module reads lease freshness: a
``running`` attempt is live whatever its owner's heartbeat says, until its own process closes it or
a user action abandons it. Freshness (:meth:`JobAttemptRecord.fresh`) is shown by the viewer and
gates a takeover in :func:`resume`, nothing else. So state moves one way: nothing ever starts work
on the assumption that a process is dead and then finds it alive.

**Steps across jobs.** A step's result is saved under a global cache key. What a job asked for at
a key (``job_step_requests``) and which result it took (``job_step_bindings``) are ledger facts;
the execution state is the *cache entry* (``step_cache``: which request currently owns the key,
and either which attempt is *current* for it or which step result it *offers*) and the step
attempts. A step attempt that completes records a step result (``step_results``, a ledger fact
with the attempt's id) and moves the entry from itself to that step result. Jobs coordinate only
through the entry, by :func:`resolve_step`, for a job ``J`` requesting ``R`` at key ``K``:

1. ``J`` has a binding at ``K``: :class:`Bound`. Done.
2. No entry: :class:`RunIt`; starting creates the entry with ``R``.
3. An entry with a *different* request that offers a step result or whose current attempt is
   running: a *cache conflict*, :class:`ConflictingEntry` (an interruption of the requesting run
   node, fixable by clearing the key and resuming). If its attempt failed or was abandoned, nothing
   live owns the key: ``RunIt`` takes the entry over. Other jobs' bindings are untouched. If the
   job's active job attempt overwrites cache conflicts
   (``job_attempts.overwrite_cache_conflicts``), an entry that offers a different request's step
   result is not a conflict: ``RunIt`` overwrites it (``RunIt.overwrites``), as a clear and then a
   run would. A running attempt of a different request is still a conflict.
4. An entry with an *equal* request: its attempt running: :class:`Pending`, another job is
   computing it; a step result on offer: :class:`Completed`, and ``J`` *takes* it by writing its
   own binding (:func:`take_step_result`), after which it is ``Bound``; its attempt failed or
   abandoned: ``RunIt``.

What a job makes of its steps as a whole, and what it displays for them, are judged from the same
rules restated relationally (:mod:`fxtr.runner.postgres.status_queries`), so that many keys are
judged in one query; the two statements of the rules must agree.

**A job writes its own bindings.** Completing a step binds nobody; a job binds when its evaluator
sees ``Completed``, in a write transaction that resolves again first (so a key cleared in between
is not bound to). One writer per binding row, the job itself, so a request recorded concurrently
with the completion cannot fall between two writers, and a job proceeds only once its binding is
in the ledger. Nothing here reports a step done from the cache entry; only from the binding.

**Concurrency.** Transactions run at the default isolation level and take no advisory locks and no
``FOR UPDATE``; a row is locked by a conditional ``UPDATE`` on it, which blocks behind an in-flight
writer of the row and re-evaluates its predicate once that writer commits, so whoever runs second
sees the first's result (a no-op ``SET state = state`` locks like any other). Two rules make every
writer sound, and rows are always taken in this order: **the job's row, then the job attempt's
row, then attempt rows, then the cache entry**. The order is what rules out deadlock: an eviction
and an ending both take the job attempt row, then attempt rows; a start and a settle take the job
attempt row and then, for a start, insert an attempt row; and a transaction that takes only an
attempt row holds nothing above it.

* *Every writer of a job's execution state takes the job attempt's row first*: starting an attempt
  (on ``state = 'active' AND evicted_at IS NULL``), settling, cancelling, evicting,
  heartbeating. :func:`resume` takes the job's row before that, so two resumes of one job
  serialize. *First* means before any row the transaction writes or locks; plain reads may come
  earlier, and the transactions every step makes (its start and its ending) do their reads
  there, so that the row, which every step of the job takes twice, is held only across their
  writes and the commit. A read made before the row is taken can be overtaken by a commit that
  the row would have waited for, so a writer reads early only where each write acting on the read
  is conditional on it still holding (see :func:`start_attempt`). An attempt's own transactions
  take only their attempt row (:func:`guard`), with one exception: **the transaction that ends
  an attempt** (completing it, or closing it failed, interrupted, or abandoned) takes the
  owner's job attempt row first, whatever its state (``guard(..., ending=True)``), then the
  attempt row. Ending is the one attempt-side transition
  :func:`settle` derives its verdict from (is anything running, is the root finished, is anything
  due), and settle reads those in several statements at read committed, so an ending that could
  commit between them would let settle see an attempt as not running *and* its invocation as
  unfinished and stop a job that succeeded. Recording, expanding, and binding need no such lock:
  they are written by a running attempt, and a running attempt is itself what keeps settle from
  concluding. Closing still does not settle; the owning runner does. The row is taken whatever the
  job attempt's state because a job attempt settled ``cancelled`` drains, and its attempts must
  still be able to end.
* *Every write at a cache key is one atomic conditional statement*: creating the entry is an insert
  with on-conflict-do-nothing, taking it over is an update conditional on the previous current
  attempt, clearing it is a delete; and a binding is written only by its own job. No key write
  depends on a read it could race, so no per-key lock is needed.

What that gives, writer by writer: a guard and an eviction of one attempt serialize on the attempt
row and whichever runs second sees the other's state; an ending and a settle serialize on the job
attempt row, so a settle sees an attempt either still running or ended with its invocation's
result in place, never in between; a start and an evict, cancel, or settle
serialize on the job attempt row, so a start that loses finds the job attempt no longer active or
evicted and starts nothing, and an evict that wins sweeps everything that was running and nothing
can be added after it; two starters of one invocation collide on the one-running index; two jobs
taking over one key collide on the entry's conditional update; a takeover is conditional on the
heartbeat still being stale, so a heartbeat that lands first keeps the lease, and a heartbeat that
finds no row to update has, by construction, been evicted. Readers that compose several queries
into one view open a ``snapshot=True`` transaction and never write.

Every function does one logical thing against the database given a
:class:`~fxtr.runner.postgres.database.Transaction`, never opens one, and checks its own
preconditions inside it. Transitions that change what is pending wake the relevant topics on commit
(:meth:`~fxtr.runner.postgres.database.Transaction.wake`): a job's topic when something of the job
changed, a cache key's topic when its current attempt ended or the key was cleared.

**How to read this module.** After the errors and record types: *job attempts and the lease*
(create, find the active one, the row lock, heartbeat, evict, cancel);
*what is due* (:func:`pending_work`, :func:`start_attempt`); *attempts*, in the order one goes
through them (guarded on every write, materializing a result or closing without one) and
:func:`settle`; *steps* (the cache, :func:`resolve_step`, :func:`take_step_result`, step
attempts); *status* and *step_status* and the derived invocation state; *resume*; then the readers
and the row decoding helpers.
"""

from __future__ import annotations

import dataclasses
import itertools
import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from frozendict import frozendict
from sqlalchemy import Row, and_, case, func, null, or_, select, text, tuple_, update

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID, cid_from_str, cid_to_str
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.project.client import CacheEntry, CacheEntryState
from fxtr.project.code_version import CodeVersion
from fxtr.runner import nodes
from fxtr.runner.cache_addresses import parse_pattern
from fxtr.runner.nodes import NodeIdentity
from fxtr.runner.postgres import ledger
from fxtr.runner.postgres.database import Transaction
from fxtr.runner.postgres.ledger import (
    ActiveAttempt,
    InvocationID,
    InvocationRecord,
    JobAttemptID,
    JobID,
    LedgerError,
    NotFoundError,
    SpawnRecord,
    StepResultID,
    attempt_uuid,
    bound_array,
    identity_from_bytes,
)
from fxtr.runner.postgres.schema import (
    attempts,
    job_attempt_provenance,
    job_attempts,
    jobs,
    step_cache,
    step_requests,
    step_results,
)
from fxtr.runner.postgres.status_queries import StatusQueries, StepStateRecord
from fxtr.runner.session import AttemptError, AttemptID

type JobAttemptState = Literal["active", "succeeded", "stopped", "cancelled"]
"""The states of ``job_attempts.state``; see the schema for what each means."""
type AttemptKind = Literal["workflow", "step"]
"""The kinds of ``attempts.kind``: a call of a workflow body, or of a step."""
type AttemptState = Literal["running", "completed", "failed", "interrupted", "abandoned"]
"""The states of ``attempts.state``; see the schema for what each means."""
type ClosedState = Literal["failed", "interrupted", "abandoned"]
"""The states an attempt can be closed in without finishing its invocation."""
type InvocationState = Literal["queued", "running", "completed", "failed", "interrupted", "stopped"]
"""An invocation's derived status; see :func:`invocation_state`."""


def job_topic(job_id: JobID) -> str:
    """The wake topic set when something of a job changes state."""
    return f"job:{job_id}"


def step_topic(cache_key: str) -> str:
    """The wake topic set when a cache key's current attempt ends or the key is cleared."""
    return f"step:{cache_key}"


# ---- errors ----


class StaleAttemptError(LedgerError):
    """The attempt may not write: it is not running. Someone else closed it (an eviction, a hard
    cancel) or it already ended."""


class NotResumableError(LedgerError):
    """The invocation is finished, so there is nothing to resume. Raised by the commands over
    :func:`resume`, which itself reports the case as :class:`AlreadyFinished`."""


class NotCancellableError(LedgerError):
    """The job has no active job attempt to cancel."""


class JobRunningElsewhereError(LedgerError):
    """The job's active job attempt holds a fresh lease under another owner: another process is
    running the job, so this one may not."""


# ---- records ----


@dataclasses.dataclass(frozen=True)
class JobAttemptRecord:
    """One row of ``job_attempts``; see the schema for the meaning of each field.

    Attributes:
        id: The job attempt.
        job_id: Its job.
        seq: Its position among the job's job attempts, from 1, in creation order; derived from
            ``created_at`` (ties broken by id) when read, not stored.
        state: Its state.
        reason: For ``cancelled``, why.
        owner: The process that runs it.
        code_version: The code its process runs (see the ``job_attempts`` table); every attempt
            the job attempt owns ran at it.
        heartbeat_at: When the owner last refreshed the lease.
        evicted_at: When it was evicted, if it was.
        evicted_reason: For ``evicted_at``, why.
        created_at: When it was created.
        settled_at: When it left ``active``.
        read_at: The database's clock when this record was read, for :meth:`fresh`.
        overwrite_cache_conflicts: Whether, while it is active, the requests of its job overwrite
            cache conflicts with completed results (see :func:`resolve_step`).
    """

    id: JobAttemptID
    job_id: JobID
    seq: int
    state: JobAttemptState
    reason: str | None
    owner: str
    code_version: BoundID[CodeVersion]
    heartbeat_at: datetime
    evicted_at: datetime | None
    evicted_reason: str | None
    created_at: datetime
    settled_at: datetime | None
    read_at: datetime
    overwrite_cache_conflicts: bool = False

    @property
    def active(self) -> bool:
        return self.state == "active"

    @property
    def evicted(self) -> bool:
        return self.evicted_at is not None

    def fresh(self, stale_after: timedelta) -> bool:
        """Whether the lease is fresh: not evicted, and heartbeaten within ``stale_after`` of when
        this record was read. Independent of the state (a cancelled job attempt stays fresh while
        its process drains). Information for the viewer and for :func:`resume`; no decision about
        what runs reads it. The threshold is the caller's: the policy has no opinion on how long
        a process may go quiet, since one process's heartbeat is judged by another's threshold and
        every process of a project must agree (the project's configuration says)."""
        return not self.evicted and self.read_at - self.heartbeat_at < stale_after


@dataclasses.dataclass(frozen=True)
class ActiveStepAttempt:
    """What :func:`guard_step` returns: a running step attempt that may write.

    Attributes:
        id: The attempt.
        cache_key: The key its result is saved under.
        request: The request it executes (its fingerprint).
        owner_job_attempt_id: The job attempt whose process runs it.
        ending: Whether the guard took the owner's job attempt row first, which the transaction
            that ends the attempt must (see the module docstring).
    """

    id: AttemptID
    cache_key: str
    request: EntityID
    owner_job_attempt_id: JobAttemptID
    ending: bool = False


@dataclasses.dataclass(frozen=True)
class CacheEntryRecord:
    """One row of ``step_cache``: the request that owns a cache key, and either the attempt current
    for it or the step result it offers (exactly one is set).

    Attributes:
        cache_key: The key.
        request: The request that owns the key (its fingerprint).
        attempt_id: The current attempt for it, until one completes.
        step_result_id: The step result it offers, once its attempt completed or a job's result
            was adopted there.
    """

    cache_key: str
    request: EntityID
    attempt_id: AttemptID | None
    step_result_id: StepResultID | None


@dataclasses.dataclass(frozen=True)
class AttemptRecord:
    """One row of ``attempts``; see the schema for the meaning of each field.

    Attributes:
        id: The attempt.
        kind: A workflow body's call or a step's.
        invocation_id: For a workflow attempt, the invocation it runs; ``None`` for a step.
        cache_key: For a step attempt, the key its result is saved under; ``None`` for a workflow.
        request: For a step attempt, the request it executes (its fingerprint); ``None`` for a
            workflow.
        owner_job_attempt_id: The job attempt whose process runs (ran) it.
        state: Its state.
        result: For a completed step attempt, its result, read from the step result with its id;
            ``None`` otherwise (a workflow's result is on its invocation).
        error: For state ``failed``, what went wrong.
        reason: For states ``interrupted`` and ``abandoned``, why.
        stopped_on: The nodes the attempt stopped on, when a node's state is what stopped it (see
            the schema); ``None`` otherwise.
        started_at: When it started running.
        finished_at: When it left ``running``.
    """

    id: AttemptID
    kind: AttemptKind
    invocation_id: InvocationID | None
    cache_key: str | None
    request: EntityID | None
    owner_job_attempt_id: JobAttemptID
    state: AttemptState
    result: BoundID[Array[Any]] | None
    error: AttemptError | None
    reason: str | None
    stopped_on: tuple[NodeIdentity, ...] | None
    started_at: datetime
    finished_at: datetime | None

    @property
    def live(self) -> bool:
        """Whether the attempt is running."""
        return self.state == "running"


@dataclasses.dataclass(frozen=True)
class StartInvocation:
    """A unit of due work: run a workflow invocation."""

    invocation_id: InvocationID


@dataclasses.dataclass(frozen=True)
class StartStep:
    """A unit of due work: run the job's step request at a cache key."""

    cache_key: str


type WorkItem = StartInvocation | StartStep
"""What :func:`pending_work` lists and :func:`start_attempt` starts."""


@dataclasses.dataclass(frozen=True)
class Started:
    """What :func:`start_attempt` hands back for a workflow attempt it started.

    Attributes:
        attempt_id: The attempt, now running under the job attempt.
        invocation: The invocation.
        inputs: Its arguments, by name: the identity and type of each ``input`` node.
        nodes: Every node the invocation already has (for a resumed invocation, what earlier
            attempts recorded), so the attempt's view can be seeded before the body runs.
        spawns: What each of its run nodes has already expanded into, by node identity, likewise
            a seed for the view (the table itself keeps a resumed attempt from expanding a node
            twice).
    """

    attempt_id: AttemptID
    invocation: InvocationRecord
    inputs: frozendict[str, tuple[NodeIdentity, ArrayType]]
    nodes: tuple[ledger.NodeRecord, ...]
    spawns: frozendict[NodeIdentity, tuple[SpawnRecord, ...]]


@dataclasses.dataclass(frozen=True)
class StartedStep:
    """What :func:`start_attempt` hands back for a step attempt it started.

    Attributes:
        attempt: The attempt, now running and the cache entry's current; its ``cache_key`` and
            ``request`` (the fingerprint, an entity holding the step's inputs) are what the
            assignment is built from.
        step_name: The step to run.
        overwrote: The step result of a different request that the attempt took the entry from,
            if it overwrote a cache conflict. ``None`` otherwise.
    """

    attempt: AttemptRecord
    step_name: str
    overwrote: StepResultID | None = None


@dataclasses.dataclass(frozen=True)
class Bound:
    """The job's request at the key is bound to this result: a ledger fact, whatever happens to
    the cache entry afterwards.

    Attributes:
        result: The result.
        step_result_id: The step result it is.
    """

    result: BoundID[Array[Any]]
    step_result_id: StepResultID


@dataclasses.dataclass(frozen=True)
class Completed:
    """The cache entry offers a step result of an equal request and the job has not taken it:
    :func:`take_step_result` writes the binding, after which the key resolves :class:`Bound`.
    Nothing reports the step done from this; only from the binding.

    Attributes:
        result: The result on offer.
        step_result_id: The step result the entry offers.
    """

    result: BoundID[Array[Any]]
    step_result_id: StepResultID


@dataclasses.dataclass(frozen=True)
class RunIt:
    """Nothing live answers the request: the job should run the step itself.

    Attributes:
        request: The request to run (the job's own).
        replaces: The cache entry's current attempt this run would replace, or ``None`` for a
            free key. What :func:`start_attempt` expects to find when it takes the entry.
        overwrites: The step result of a different request that this run would overwrite,
            because the active job attempt overwrites cache conflicts. ``None`` if the run
            overwrites nothing. If it is set, ``replaces`` is ``None``, and
            :func:`start_attempt` takes the entry only while the entry offers this step result.
    """

    request: EntityID
    replaces: AttemptID | None
    overwrites: StepResultID | None = None


@dataclasses.dataclass(frozen=True)
class Pending:
    """The target is being produced or is due: ``attempt_id`` names the running attempt, or is
    ``None`` when nothing runs it yet and the active job attempt's process is to start it."""

    attempt_id: AttemptID | None = None


@dataclasses.dataclass(frozen=True)
class ConflictingEntry:
    """A cache conflict. The entry at the key belongs to a different request, from another job.
    That request is completed or its attempt is live. Thus this request cannot run at the key
    until the key is cleared.

    This is not the same as two different requests at one key in one job. That is a fault of the
    body, and it causes :class:`~fxtr.runner.postgres.ledger.CacheAddressReuseError`."""

    entry: CacheEntryRecord


type StepResolution = Bound | Completed | RunIt | Pending | ConflictingEntry
"""What :func:`resolve_step` says about a job's request at a key."""


@dataclasses.dataclass(frozen=True)
class Done:
    """The target has its result."""

    result: BoundID[Array[Any]]


@dataclasses.dataclass(frozen=True)
class Stopped:
    """The target will not get a result unless a person acts: nothing is handling it. Whatever
    asked is interrupted by it.

    Attributes:
        reason: Why: a failed or interrupted attempt of it, a cache conflict, no active job
            attempt.
        error: The target's own error, when it failed.
        attempt_id: The attempt whose failure or interruption this is, when there is one.
        cache_conflict: Whether the stop is a cache conflict: a different request owns the key.
    """

    reason: str
    error: AttemptError | None = None
    attempt_id: AttemptID | None = None
    cache_conflict: bool = False


@dataclasses.dataclass(frozen=True)
class Ready:
    """The cache entry offers a step result of an equal request and the job has not yet taken it:
    the asking attempt binds through :func:`take_step_result`, then the target is :class:`Done`.
    Not done yet: until the binding is written, a cleared key would lose it.

    Attributes:
        step_result_id: The step result on offer.
    """

    step_result_id: StepResultID


type TargetStatus = Done | Ready | Pending | Stopped
"""What :func:`status` and :func:`step_status` say about a target a run node waits on."""


@dataclasses.dataclass(frozen=True)
class AlreadyFinished:
    """What :func:`resume` returns for a job whose root has its result: nothing to resume.

    Attributes:
        invocation: The root invocation, finished, with its result.
        job_attempt: The job's latest job attempt, settled (by its runner, or by this resume if
            its process died first); ``None`` for a job that never had one.
    """

    invocation: InvocationRecord
    job_attempt: JobAttemptRecord | None


# ---- job attempts and the lease ----


async def create_job_attempt(
    tx: Transaction,
    job_id: JobID,
    *,
    owner: str,
    code_version: BoundID[CodeVersion],
    overwrite_cache_conflicts: bool = False,
) -> JobAttemptRecord:
    """Start a new job attempt for a job, as its active one, owned by ``owner`` with a fresh
    lease, recording ``code_version``.

    The caller must have established that the job has no active job attempt (a second active
    one is refused by the database). The code version must be in the entity store (flushed in
    this transaction or committed earlier).

    Args:
        job_id: The job.
        owner: The process that will run it.
        code_version: The code that process runs.
        overwrite_cache_conflicts: If ``True``, the requests of the job overwrite cache
            conflicts with completed results while this job attempt is active (see
            :func:`resolve_step`). Only this job attempt does so, not a later one.
    """
    job_attempt_id = JobAttemptID(uuid.uuid4())
    await tx.execute(
        job_attempt_provenance.insert().values(
            id=job_attempt_id,
            job_id=job_id,
            code_version_cid=code_version.entity_id.cid,
            # The insert's wall clock, not the transaction's start: a resume that waited on a lock
            # behind the job attempt before it still sorts after it.
            created_at=func.clock_timestamp(),
        )
    )
    await tx.execute(
        job_attempts.insert().values(
            id=job_attempt_id,
            job_id=job_id,
            state="active",
            owner=owner,
            overwrite_cache_conflicts=overwrite_cache_conflicts,
        )
    )
    tx.wake(job_topic(job_id))
    return await get_job_attempt(tx, job_attempt_id)


async def active_job_attempt(tx: Transaction, job_id: JobID) -> JobAttemptRecord | None:
    """The job's active job attempt, or ``None`` if it has none."""
    row = (
        await tx.execute(
            _job_attempt_query().where(
                job_attempts.c.job_id == job_id, job_attempts.c.state == "active"
            )
        )
    ).first()
    return None if row is None else await _job_attempt(tx, row)


async def lock_job_attempt(
    tx: Transaction, job_attempt_id: JobAttemptID, *, evicted_ok: bool = False
) -> JobAttemptRecord | None:
    """Take the job attempt's row for the rest of the transaction, if it is still active (and not
    evicted, unless ``evicted_ok``): the first statement of every writer of the job's execution
    state (see the module docstring). A conditional no-op update: it waits for an in-flight writer
    of the row and re-checks afterwards.

    Returns:
        The job attempt as it is now, or ``None`` if it is no longer active (or was evicted).
    """
    if not await _take_job_attempt(tx, job_attempt_id, evicted_ok=evicted_ok):
        return None
    return await get_job_attempt(tx, job_attempt_id)


async def _take_job_attempt(
    tx: Transaction, job_attempt_id: JobAttemptID, *, evicted_ok: bool = False
) -> bool:
    """:func:`lock_job_attempt` without reading the row back, for a writer that needs nothing of
    it but the lock and that it is still active (and not evicted, unless ``evicted_ok``)."""
    conditions = [job_attempts.c.id == job_attempt_id, job_attempts.c.state == "active"]
    if not evicted_ok:
        conditions.append(job_attempts.c.evicted_at.is_(None))
    taken = await tx.execute(
        update(job_attempts).where(*conditions).values(state=job_attempts.c.state)
    )
    return taken.rowcount > 0


async def _lock_active_job_attempt(tx: Transaction, job_id: JobID) -> JobAttemptRecord | None:
    """Take the row of the job's active job attempt, whichever it is, or ``None`` if there is
    none (see :func:`lock_job_attempt`)."""
    row = (
        await tx.execute(
            update(job_attempts)
            .where(job_attempts.c.job_id == job_id, job_attempts.c.state == "active")
            .values(state=job_attempts.c.state)
            .returning(job_attempts.c.id)
        )
    ).first()
    return None if row is None else await get_job_attempt(tx, JobAttemptID(row[0]))


async def _lock_job(tx: Transaction, job_id: JobID) -> None:
    """Take the job's own row for the rest of the transaction, so that operations that may create
    its next job attempt (:func:`resume`) serialize.

    Raises:
        NotFoundError: If there is no such job.
    """
    taken = await tx.execute(
        update(jobs).where(jobs.c.id == job_id).values(workflow_name=jobs.c.workflow_name)
    )
    if taken.rowcount == 0:
        raise NotFoundError(f"No job {job_id}")


async def heartbeat(
    tx: Transaction, job_attempt_id: JobAttemptID, owner: str
) -> JobAttemptState | None:
    """Refresh the lease: a conditional update on the id, the owner, and the job attempt not
    being evicted.

    Returns:
        The job attempt's state (``active``, or a settled state the owner should notice, a soft
        cancel in particular), or ``None`` if the job attempt was evicted, or never existed under
        this owner: the caller has lost the job.
    """
    row = (
        await tx.execute(
            update(job_attempts)
            .where(
                job_attempts.c.id == job_attempt_id,
                job_attempts.c.owner == owner,
                job_attempts.c.evicted_at.is_(None),
            )
            .values(heartbeat_at=func.now())
            .returning(job_attempts.c.state)
        )
    ).first()
    return None if row is None else row[0]


async def evict(
    tx: Transaction,
    job_attempt: JobAttemptRecord,
    reason: str,
    *,
    only_if_stale: timedelta | None = None,
    settle_as: Literal["stopped", "cancelled"] = "stopped",
) -> list[AttemptID] | None:
    """Take the job attempt's work away from its process: mark it evicted (a conditional update on
    its row, so two evictors do not both win, and the row lock every later start waits on), close
    every running attempt it owns as abandoned, and settle it ``settle_as`` if it is still active.
    Its process finds out on its next heartbeat; any write it tries afterwards is refused by the
    guard, since its attempts are no longer running, and nothing can be started under it, since
    the row says evicted.

    Args:
        job_attempt: The job attempt, as read earlier.
        reason: Why, recorded on it and on every attempt abandoned.
        only_if_stale: For a takeover: evict only if the heartbeat is still older than this when
            the update runs, so a heartbeat that commits first keeps the lease.
        settle_as: The state to settle it in if it is still active (a hard cancel has settled it
            ``cancelled`` already).

    Returns:
        The attempts abandoned, or ``None`` if nothing was evicted: the job attempt already was, or
        its heartbeat turned out fresh.
    """
    conditions = [job_attempts.c.id == job_attempt.id, job_attempts.c.evicted_at.is_(None)]
    if only_if_stale is not None:
        conditions.append(job_attempts.c.heartbeat_at < func.now() - only_if_stale)
    marked = await tx.execute(
        update(job_attempts).where(*conditions).values(evicted_at=func.now(), evicted_reason=reason)
    )
    if marked.rowcount == 0:
        return None
    # The row is ours now: no start can add to what it owns, and every running attempt is read
    # here as committed. An attempt closing itself concurrently holds only its own row; the close
    # below waits for it and then finds nothing to do.
    running = (
        await tx.execute(
            select(attempts.c.id).where(
                attempts.c.owner_job_attempt_id == job_attempt.id, attempts.c.state == "running"
            )
        )
    ).scalars()
    closed: list[AttemptID] = []
    for running_uuid in list(running):
        attempt_id = AttemptID(str(running_uuid))
        if await _close(tx, attempt_id, "abandoned", reason=reason):
            closed.append(attempt_id)
    await tx.execute(
        update(job_attempts)
        .where(job_attempts.c.id == job_attempt.id, job_attempts.c.state == "active")
        .values(state=settle_as, settled_at=func.now())
    )
    tx.wake(job_topic(job_attempt.job_id))
    return closed


async def cancel(
    tx: Transaction, job_attempt: JobAttemptRecord, *, hard: bool, reason: str
) -> None:
    """Cancel an active job attempt: it stops deciding. Nothing unfinished of the job is pending
    any more (:func:`status`, :func:`step_status`), and the owning process, seeing the new state on
    its next heartbeat, starts nothing more. Under a *soft* cancel what runs finishes (results and
    bindings are facts; a workflow attempt is interrupted at its next observe or return of
    anything unfinished). A *hard* cancel also evicts the job attempt, abandoning what it owns. The
    ledger is untouched either way; a resume starts the next job attempt.

    Args:
        job_attempt: The job's active job attempt.
        hard: Whether to evict too.
        reason: Why, recorded on the job attempt (and on the attempts a hard cancel abandons).

    Raises:
        NotCancellableError: If the job attempt is not active.
    """
    settled = await tx.execute(
        update(job_attempts)
        .where(job_attempts.c.id == job_attempt.id, job_attempts.c.state == "active")
        .values(state="cancelled", reason=reason, settled_at=func.now())
    )
    if settled.rowcount == 0:
        raise NotCancellableError(f"Job attempt {job_attempt.id} is not active")
    tx.wake(job_topic(job_attempt.job_id))
    if hard:
        await evict(tx, job_attempt, f"cancelled: {reason}", settle_as="cancelled")


# ---- what is due ----


async def pending_work(tx: Transaction, job_attempt: JobAttemptRecord) -> list[WorkItem]:
    """Everything the job attempt is due to start: the derivable queue (see the module docstring).

    The job's invocations that are unfinished, not running, and that the job attempt has not tried
    except for abandoned attempts; and the job's unbound step requests that :func:`resolve_step`
    says to run, except keys the job attempt already ran a failed attempt for. Invocations first,
    each group in creation order. Nothing is written.

    What is due is read as the ``queued`` rows of the status relations
    (:class:`~fxtr.runner.postgres.status_queries.StatusQueries`), in two queries whatever the
    job's size, because :func:`settle` derives the runner's queue from this while holding the job
    attempt's row, which every start and ending of the job's attempts waits for. The relations
    judge the job's active job attempt as the database has it, so ``job_attempt`` must be that job
    attempt, as it is under :func:`settle`'s lock; a record that is settled or evicted is due
    nothing.
    """
    if not job_attempt.active or job_attempt.evicted:
        return []
    queries = StatusQueries([job_attempt.job_id])
    invocations = queries.invocations
    steps = queries.steps
    invocation_ids = (
        await tx.execute(
            select(invocations.c.id)
            .where(invocations.c.display_state == "queued")
            .order_by(invocations.c.created_at, invocations.c.pathname)
        )
    ).scalars()
    cache_keys = (
        await tx.execute(
            select(steps.c.cache_key)
            .where(steps.c.target_state == "queued")
            .order_by(steps.c.requested_at, steps.c.cache_key)
        )
    ).scalars()
    return [
        *(StartInvocation(InvocationID(invocation_id)) for invocation_id in invocation_ids),
        *(StartStep(cache_key) for cache_key in cache_keys),
    ]


async def _running_attempt(tx: Transaction, invocation_id: InvocationID) -> AttemptRecord | None:
    row = (
        await tx.execute(
            _attempt_query().where(
                attempts.c.invocation_id == invocation_id, attempts.c.state == "running"
            )
        )
    ).first()
    return None if row is None else _attempt(row)


async def _failed_here(tx: Transaction, job_attempt_id: JobAttemptID, cache_key: str) -> bool:
    """Whether the job attempt already ran a step attempt at the key that failed (it does not
    retry within itself)."""
    row = (
        await tx.execute(
            select(attempts.c.id)
            .where(
                attempts.c.owner_job_attempt_id == job_attempt_id,
                attempts.c.cache_key == cache_key,
                attempts.c.state == "failed",
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def start_attempt(
    tx: Transaction,
    job_attempt: JobAttemptRecord,
    item: WorkItem,
) -> Started | StartedStep | None:
    """Start one unit of due work under the job attempt: insert its attempt ``running`` and hand
    back what running it needs. Idempotent and safe to call on stale information: returns
    ``None`` when there is nothing to start after all (the job attempt is no longer active or was
    evicted, the invocation finished or is running, the job attempt already has a failed attempt
    at the step's key, the step request resolved to something other than running it, or another
    process took the cache entry first).

    Takes the job attempt's row before inserting the attempt (:func:`lock_job_attempt`), so a
    start waits for an in-flight evict, cancel, or settle of the job attempt and then sees it. A
    workflow attempt takes it first. A step attempt, started once per step, decides whether to
    run from plain reads (its request, its earlier failures here, :func:`resolve_step`) and only
    then takes the row, for its two writes: a read overtaken by a commit in between starts
    nothing, since taking the row re-checks the job attempt and taking the cache entry is
    conditional on the entry as it was read. Within the job attempt nothing can overtake those
    reads: its runner starts a key only once nothing is live there
    (:meth:`~fxtr.runner.postgres.runner.JobRunner.enqueue`).

    Args:
        job_attempt: The job attempt, as read earlier; its state is re-checked here.
        item: What to start.
    """
    match item:
        case StartInvocation(invocation_id=invocation_id):
            current = await lock_job_attempt(tx, job_attempt.id)
            if current is None:
                return None
            return await _start_workflow_attempt(tx, current, invocation_id)
        case StartStep(cache_key=cache_key):
            return await _start_step_attempt(tx, job_attempt, cache_key)


async def _start_workflow_attempt(
    tx: Transaction,
    job_attempt: JobAttemptRecord,
    invocation_id: InvocationID,
) -> Started | None:
    invocation = await ledger.get_invocation(tx, invocation_id)
    if invocation.finished or invocation.job_id != job_attempt.job_id:
        return None
    attempt_id = AttemptID(str(uuid.uuid4()))
    row = (
        await tx.execute(
            tx.upsert(attempts)
            .values(
                id=attempt_uuid(attempt_id),
                kind="workflow",
                invocation_id=invocation_id,
                owner_job_attempt_id=job_attempt.id,
                state="running",
            )
            .on_conflict_do_nothing(
                index_elements=[attempts.c.invocation_id],
                # The index's own predicate, as literal SQL: bound as a parameter, it stops
                # matching the partial index once Postgres plans the statement generically.
                index_where=text("state = 'running'"),
            )
            .returning(attempts.c.id)
        )
    ).first()
    if row is None:
        return None
    tx.wake(job_topic(job_attempt.job_id))
    records = await ledger.load_nodes(tx, invocation_id)
    inputs = {
        record.node.name: (record.identity, record.type)
        for record in records
        if isinstance(record.node, nodes.Input)
    }
    spawns: dict[NodeIdentity, list[SpawnRecord]] = {}
    for spawn in await ledger.load_spawns(tx, invocation_id):
        spawns.setdefault(spawn.node_identity, []).append(spawn)
    return Started(
        attempt_id=attempt_id,
        invocation=invocation,
        inputs=frozendict(inputs),
        nodes=tuple(records),
        spawns=frozendict({identity: tuple(rows) for identity, rows in spawns.items()}),
    )


async def _start_step_attempt(
    tx: Transaction,
    job_attempt: JobAttemptRecord,
    cache_key: str,
) -> StartedStep | None:
    request = await ledger.get_job_step_request(tx, job_attempt.job_id, cache_key)
    if request is None:
        return None
    # A StartStep can be queued before this job attempt's own attempt at the key fails and be
    # handled after, and a job attempt does not retry its own failure.
    if await _failed_here(tx, job_attempt.id, cache_key):
        return None
    resolution = await resolve_step(tx, job_attempt.job_id, cache_key, request.request)
    if not isinstance(resolution, RunIt):
        return None
    spec = await ledger.get_step_request(tx, resolution.request)
    # Everything above is a plain read; the row is taken for the writes (see start_attempt).
    if not await _take_job_attempt(tx, job_attempt.id):
        return None
    attempt_id = AttemptID(str(uuid.uuid4()))
    inserted = (
        await tx.execute(
            attempts.insert()
            .values(
                id=attempt_uuid(attempt_id),
                kind="step",
                cache_key=cache_key,
                request_cid=resolution.request.cid,
                owner_job_attempt_id=job_attempt.id,
                state="running",
            )
            # The columns of _attempt_query, a new attempt having no step result.
            .returning(*attempts.c, null().label("result_cid"))
        )
    ).one()
    # Take the entry: create it if the key is free, or replace exactly the current attempt or the
    # overwritten step result the resolution saw. Anything else means another process got there
    # first; give the attempt back.
    if resolution.overwrites is not None:
        taken = await tx.execute(
            update(step_cache)
            .where(
                step_cache.c.cache_key == cache_key,
                step_cache.c.step_result_id == resolution.overwrites,
            )
            .values(
                request_cid=resolution.request.cid,
                attempt_id=attempt_uuid(attempt_id),
                step_result_id=None,
                updated_at=func.now(),
            )
        )
        # A job that was about to take the overwritten result looks again.
        tx.wake(step_topic(cache_key))
    elif resolution.replaces is None:
        taken = await tx.execute(
            tx.upsert(step_cache)
            .values(
                cache_key=cache_key,
                request_cid=resolution.request.cid,
                attempt_id=attempt_uuid(attempt_id),
            )
            .on_conflict_do_nothing(index_elements=[step_cache.c.cache_key])
        )
    else:
        taken = await tx.execute(
            update(step_cache)
            .where(
                step_cache.c.cache_key == cache_key,
                step_cache.c.attempt_id == attempt_uuid(resolution.replaces),
            )
            .values(
                request_cid=resolution.request.cid,
                attempt_id=attempt_uuid(attempt_id),
                updated_at=func.now(),
            )
        )
    if taken.rowcount == 0:
        await tx.execute(attempts.delete().where(attempts.c.id == attempt_uuid(attempt_id)))
        return None
    tx.wake(job_topic(job_attempt.job_id))
    return StartedStep(
        attempt=_attempt(inserted),
        step_name=spec.step_name,
        overwrote=resolution.overwrites,
    )


# ---- attempts, in the order one goes through them ----


async def guard(tx: Transaction, attempt_id: AttemptID, *, ending: bool = False) -> ActiveAttempt:
    """Check a workflow attempt may write (it is running; see the module docstring) and lock its
    row for the transaction. Every transaction acting for a workflow attempt starts with this; the
    result is what the ledger's writes take.

    Args:
        ending: The transaction ends the attempt (:func:`materialize_result` or
            :func:`close_attempt`): take the owner's job attempt row first, whatever its state, so
            the ending serializes with :func:`settle`. Every other transaction takes only the
            attempt's own row.

    Raises:
        StaleAttemptError: If the attempt is unknown, not a workflow attempt, or not running.
    """
    row = await _guard_row(tx, attempt_id, "workflow", ending=ending)
    assert row.invocation_id is not None
    return ActiveAttempt(
        id=attempt_id,
        invocation_id=InvocationID(row.invocation_id),
        job_id=JobID(row.job_id),
        owner_job_attempt_id=JobAttemptID(row.owner_job_attempt_id),
        ending=ending,
    )


async def guard_step(
    tx: Transaction, attempt_id: AttemptID, *, ending: bool = False
) -> ActiveStepAttempt:
    """The step counterpart of :func:`guard`; ``ending`` for :func:`complete_step` and
    :func:`close_step_attempt`.

    Raises:
        StaleAttemptError: If the attempt is unknown, not a step attempt, or not running.
    """
    row = await _guard_row(tx, attempt_id, "step", ending=ending)
    assert row.cache_key is not None and row.request_cid is not None
    return ActiveStepAttempt(
        id=attempt_id,
        cache_key=row.cache_key,
        request=EntityID(cid=row.request_cid),
        owner_job_attempt_id=JobAttemptID(row.owner_job_attempt_id),
        ending=ending,
    )


@dataclasses.dataclass(frozen=True)
class _Guarded:
    """What :func:`_guard_row` establishes."""

    invocation_id: uuid.UUID | None
    job_id: uuid.UUID
    cache_key: str | None
    request_cid: bytes | None
    owner_job_attempt_id: uuid.UUID


async def _guard_row(
    tx: Transaction, attempt_id: AttemptID, kind: AttemptKind, *, ending: bool
) -> _Guarded:
    """The guard: a conditional update of the attempt row on ``state = 'running'``, which takes
    the row lock and, after blocking on a concurrent close, re-reads the row, and reads the job it
    belongs to off its owner's row (joined, which locks nothing). With ``ending``, the owner's job
    attempt row is taken before the attempt row (the lock order of the module docstring),
    unconditionally: the owner is read off the attempt row first (the column never changes) and
    its row taken with a no-op update."""
    if ending:
        owner_row = (
            await tx.execute(
                select(attempts.c.owner_job_attempt_id).where(
                    attempts.c.id == attempt_uuid(attempt_id)
                )
            )
        ).first()
        if owner_row is None:
            raise StaleAttemptError(f"Unknown attempt {attempt_id}")
        await tx.execute(
            update(job_attempts)
            .where(job_attempts.c.id == owner_row[0])
            .values(state=job_attempts.c.state)
        )
    row = (
        await tx.execute(
            update(attempts)
            .where(
                attempts.c.id == attempt_uuid(attempt_id),
                attempts.c.state == "running",
                job_attempts.c.id == attempts.c.owner_job_attempt_id,
            )
            .values(state="running")
            .returning(
                attempts.c.kind,
                attempts.c.invocation_id,
                attempts.c.cache_key,
                attempts.c.request_cid,
                attempts.c.owner_job_attempt_id,
                job_attempts.c.job_id,
            )
        )
    ).first()
    if row is None:
        found = (
            await tx.execute(
                select(attempts.c.state).where(attempts.c.id == attempt_uuid(attempt_id))
            )
        ).first()
        if found is None:
            raise StaleAttemptError(f"Unknown attempt {attempt_id}")
        raise StaleAttemptError(f"Attempt {attempt_id} is {found[0]}")
    if row.kind != kind:
        raise StaleAttemptError(f"Attempt {attempt_id} is a {row.kind} attempt")
    return _Guarded(
        invocation_id=row.invocation_id,
        job_id=row.job_id,
        cache_key=row.cache_key,
        request_cid=None if row.request_cid is None else bytes(row.request_cid),
        owner_job_attempt_id=row.owner_job_attempt_id,
    )


async def materialize_result(
    tx: Transaction, active: ActiveAttempt, result: BoundID[Array[Any]]
) -> None:
    """Record the invocation's result (:func:`~fxtr.runner.postgres.ledger.record_result`) and
    close the attempt as completed. The owning runner settles the job attempt afterwards
    (:func:`settle`).

    Args:
        active: The attempt, as ``guard(..., ending=True)`` returned it in this transaction.
        result: The returned node's value, already stored as an entity (flushed in this
            transaction or earlier).
    """
    _require_ending(active)
    await ledger.record_result(tx, active, result)
    if not await _close(tx, active.id, "completed"):
        raise StaleAttemptError(f"Attempt {active.id} is not running")


async def close_attempt(
    tx: Transaction,
    active: ActiveAttempt,
    state: ClosedState,
    *,
    error: AttemptError | None = None,
    reason: str | None = None,
    stopped_on: Sequence[NodeIdentity] = (),
) -> None:
    """Close a running workflow attempt without finishing its invocation.

    Args:
        active: The attempt, as ``guard(..., ending=True)`` returned it in this transaction.
        state: ``failed`` (with ``error``), ``interrupted`` or ``abandoned`` (with ``reason``).
        error: For ``failed``, what went wrong.
        reason: For ``interrupted`` and ``abandoned``, why.
        stopped_on: The nodes whose state stopped the attempt, if that is what did (see the
            schema's ``attempts.stopped_on``).
    """
    _require_ending(active)
    if not await _close(tx, active.id, state, error=error, reason=reason, stopped_on=stopped_on):
        raise StaleAttemptError(f"Attempt {active.id} is not running")


def _require_ending(active: ActiveAttempt | ActiveStepAttempt) -> None:
    """Ending an attempt needs the guard that took the job attempt row (see the module
    docstring); a caller that skipped it is a programming error, not a race to tolerate."""
    if not active.ending:
        raise AssertionError(
            f"Attempt {active.id} is being ended under a guard taken without ending=True"
        )


async def _close(
    tx: Transaction,
    attempt_id: AttemptID,
    state: Literal["completed", "failed", "interrupted", "abandoned"],
    *,
    error: AttemptError | None = None,
    reason: str | None = None,
    stopped_on: Sequence[NodeIdentity] = (),
) -> bool:
    """The one way an attempt leaves ``running``: set its final state with a conditional update on
    its row, then wake whoever waits on it. The caller's guard took the owner's job attempt row
    first (``ending=True``); settling the job attempt is still the owning runner's
    (:func:`settle`).

    Returns:
        Whether the attempt was running and is now closed (``False``: it had already left
        ``running``, and nothing was changed).
    """
    row = (
        await tx.execute(
            update(attempts)
            .where(
                attempts.c.id == attempt_uuid(attempt_id),
                attempts.c.state == "running",
                # Joined for the owner's job, which locks nothing.
                job_attempts.c.id == attempts.c.owner_job_attempt_id,
            )
            .values(
                state=state,
                error=None if error is None else _error_json(error),
                reason=reason,
                stopped_on=[cid_to_str(identity.cid) for identity in stopped_on]
                if stopped_on
                else None,
                finished_at=func.now(),
            )
            .returning(attempts.c.kind, attempts.c.cache_key, job_attempts.c.job_id)
        )
    ).first()
    if row is None:
        return False
    if row.kind == "step":
        assert state != "interrupted", "step attempts fail or are abandoned, never interrupted"
        assert row.cache_key is not None
        tx.wake(step_topic(row.cache_key))
    tx.wake(job_topic(JobID(row.job_id)))
    return True


@dataclasses.dataclass(frozen=True)
class Settlement:
    """What :func:`settle` found about a job's active job attempt.

    Attributes:
        settled: The job attempt, if this call settled it (``succeeded`` or ``stopped``); ``None``
            when there is no active job attempt or the episode is still going.
        due: What the job attempt is due to start (:func:`pending_work`), as read under the row
            lock; empty when it settled or there is no active job attempt.
    """

    settled: JobAttemptRecord | None
    due: tuple[WorkItem, ...] = ()


async def settle(tx: Transaction, job_id: JobID) -> Settlement:
    """Settle the job's active job attempt if the job is finished (``succeeded``) or nothing of it
    is running and nothing is due (``stopped``), and otherwise report what is due; wake the job's
    topic either way.

    The one place the settling rule lives, and the runner's one derivation of its queue: the
    runner calls it whenever nothing it runs is live and on its reconciliation interval, enqueueing
    ``due``. A takeover or an explicit eviction settles through
    :func:`evict` instead. Takes the job attempt's row first, so a start in flight has committed
    (and is seen as running) or has not begun (and will find the job attempt settled).
    """
    tx.wake(job_topic(job_id))
    job_attempt = await _lock_active_job_attempt(tx, job_id)
    if job_attempt is None:
        return Settlement(None)
    job = await ledger.get_job(tx, job_id)
    root = await ledger.find_invocation(tx, job_id, job.root)
    due: tuple[WorkItem, ...] = ()
    if root.finished:
        state: JobAttemptState = "succeeded"
    else:
        due = tuple(await pending_work(tx, job_attempt))
        if due or await _job_has_running_attempt(tx, job_id):
            return Settlement(None, due)
        state = "stopped"
    settled = await tx.execute(
        update(job_attempts)
        .where(job_attempts.c.id == job_attempt.id, job_attempts.c.state == "active")
        .values(state=state, settled_at=func.now())
    )
    if settled.rowcount == 0:
        return Settlement(None, due)
    return Settlement(await get_job_attempt(tx, job_attempt.id))


async def _job_has_running_attempt(tx: Transaction, job_id: JobID) -> bool:
    """Whether any attempt owned by any of the job's job attempts is running (workflow or step)."""
    row = (
        await tx.execute(
            select(attempts.c.id)
            .select_from(
                attempts.join(job_attempts, attempts.c.owner_job_attempt_id == job_attempts.c.id)
            )
            .where(job_attempts.c.job_id == job_id, attempts.c.state == "running")
            .limit(1)
        )
    ).first()
    return row is not None


# ---- steps: the cache and its resolution, step attempts ----


async def resolve_step(
    tx: Transaction, job_id: JobID, cache_key: str, request: EntityID
) -> StepResolution:
    """What a job's request at a key is, by the rules in the module docstring: bound
    (:class:`Bound`), offered an equal request's step result to take (:class:`Completed`), to be
    run by the job (:class:`RunIt`), waiting on another job's running attempt (:class:`Pending`),
    or in cache conflict with a different request (:class:`ConflictingEntry`). Read-only; a
    ``running`` attempt is live whatever its owner's heartbeat says.

    The answer for the write paths, which act on it in their own transaction. Readers judging many
    steps at once read the same rules as SQL, in the step relation of
    :class:`~fxtr.runner.postgres.status_queries.StatusQueries`; a change here is a change there.
    """
    return (await resolve_steps(tx, job_id, {cache_key: request}))[cache_key]


async def resolve_steps(
    tx: Transaction, job_id: JobID, requests: Mapping[str, EntityID]
) -> dict[str, StepResolution]:
    """:func:`resolve_step` for many keys of one job, by key: the same rules, reading each kind of
    row (bindings, cache entries, offered step results, current attempts) once for all the keys
    rather than once per key.

    Args:
        job_id: The job.
        requests: The job's request (its fingerprint) at each key.
    """
    resolutions: dict[str, StepResolution] = {}
    bindings = await ledger.get_step_bindings(tx, job_id, requests.keys())
    for key, binding in bindings.items():
        resolutions[key] = Bound(binding.result, binding.step_result_id)
    entries = await get_cache_entries(tx, requests.keys() - bindings.keys())
    offered = await ledger.get_step_results(
        tx,
        {
            entry.step_result_id
            for key, entry in entries.items()
            if entry.step_result_id is not None and entry.request == requests[key]
        },
    )
    current = await get_attempts(
        tx, {entry.attempt_id for entry in entries.values() if entry.attempt_id is not None}
    )
    overwrites: bool | None = None  # read once, if some key needs it
    for key, request in requests.items():
        if key in resolutions:
            continue
        entry = entries.get(key)
        if entry is None:
            resolutions[key] = RunIt(request, replaces=None)
        elif entry.step_result_id is not None:
            if entry.request != request:
                if overwrites is None:
                    overwrites = await _overwrites_conflicts(tx, job_id)
                resolutions[key] = (
                    RunIt(request, replaces=None, overwrites=entry.step_result_id)
                    if overwrites
                    else ConflictingEntry(entry)
                )
            else:
                result = offered[entry.step_result_id]
                resolutions[key] = Completed(result.result, result.id)
        else:
            assert entry.attempt_id is not None
            attempt = current[entry.attempt_id]
            if attempt.live:
                resolutions[key] = (
                    Pending(attempt.id) if entry.request == request else ConflictingEntry(entry)
                )
            else:
                # Failed or abandoned (an attempt that completed moved the entry to its step
                # result).
                resolutions[key] = RunIt(request, replaces=attempt.id)
    return resolutions


async def _overwrites_conflicts(tx: Transaction, job_id: JobID) -> bool:
    """Tell if the active job attempt of a job overwrites cache conflicts. ``False`` if the job
    has no active job attempt."""
    row = (
        await tx.execute(
            select(job_attempts.c.overwrite_cache_conflicts).where(
                job_attempts.c.job_id == job_id, job_attempts.c.state == "active"
            )
        )
    ).first()
    return row is not None and bool(row.overwrite_cache_conflicts)


async def take_step_result(
    tx: Transaction, active: ActiveAttempt, cache_key: str
) -> BoundID[Array[Any]] | None:
    """Bind the job's request at the key to the step result the cache entry offers
    (:class:`Completed`), on behalf of the job's running attempt ``active``, and return it; or
    return the result the job is already bound to. ``None`` when there is nothing to take (the key
    resolves otherwise, or the job made no request there). The one writer of a job's bindings;
    resolves afresh in this transaction, so a key cleared since the caller looked is not bound to.

    Args:
        active: The attempt taking the result, as :func:`guard` returned it in this transaction.
        cache_key: The key.
    """
    return (await take_step_results(tx, active, [cache_key])).get(cache_key)


async def take_step_results(
    tx: Transaction, active: ActiveAttempt, cache_keys: Collection[str]
) -> dict[str, BoundID[Array[Any]]]:
    """:func:`take_step_result` for many keys, by key, in a few statements whatever their number:
    a key with nothing to take is absent.

    Args:
        active: The attempt taking the results, as :func:`guard` returned it in this transaction.
        cache_keys: The keys.
    """
    requests = await ledger.get_job_step_requests(tx, active.job_id, cache_keys)
    resolutions = await resolve_steps(
        tx, active.job_id, {key: request.request for key, request in requests.items()}
    )
    taken: dict[str, BoundID[Array[Any]]] = {}
    to_bind: dict[str, StepResultID] = {}
    for key, resolution in resolutions.items():
        match resolution:
            case Bound(result=result):
                taken[key] = result
            case Completed(result=result, step_result_id=step_result_id):
                taken[key] = result
                to_bind[key] = step_result_id
            case _:
                pass
    if to_bind:
        await ledger.bind_step_results(tx, active.job_id, to_bind)
        tx.wake(job_topic(active.job_id))
    return taken


async def complete_step(
    tx: Transaction, active: ActiveStepAttempt, result: BoundID[Array[Any]]
) -> None:
    """Close a step attempt as completed and record what it produced as a step result with the
    attempt's id, run under the attempt's owner; then move the cache entry from the attempt to the
    step result, if the attempt is still the entry's current (a key cleared meanwhile stays
    clear). Binds nobody: each job waiting on the key takes the step result itself
    (:func:`take_step_result`), woken through the key's topic.

    Args:
        active: The attempt, as ``guard_step(..., ending=True)`` returned it in this transaction.
        result: The result array, already stored as an entity (flushed in this transaction or
            earlier).
    """
    _require_ending(active)
    if not await _close(tx, active.id, "completed"):
        raise StaleAttemptError(f"Attempt {active.id} is not running")
    step_result_id = ledger.step_result_of(active.id)
    await ledger.record_step_result(
        tx, step_result_id, active.request, result, active.owner_job_attempt_id
    )
    await tx.execute(
        update(step_cache)
        .where(
            step_cache.c.cache_key == active.cache_key,
            step_cache.c.attempt_id == attempt_uuid(active.id),
        )
        .values(attempt_id=None, step_result_id=step_result_id, updated_at=func.now())
    )


async def close_step_attempt(
    tx: Transaction,
    active: ActiveStepAttempt,
    state: Literal["failed", "abandoned"],
    *,
    error: AttemptError | None = None,
    reason: str | None = None,
) -> None:
    """Close a running step attempt without a result.

    Args:
        active: The attempt, as ``guard_step(..., ending=True)`` returned it in this transaction.
        state: ``failed`` (with ``error``: the step raised) or ``abandoned`` (with ``reason``).
        error: For ``failed``, what went wrong.
        reason: For ``abandoned``, why.
    """
    _require_ending(active)
    if not await _close(tx, active.id, state, error=error, reason=reason):
        raise StaleAttemptError(f"Attempt {active.id} is not running")


async def clear_cache_entry(
    tx: Transaction, cache_key: str, *, offering: StepResultID | None = None
) -> bool:
    """Forget the cache entry at a key, so that the next request there starts fresh. Existing
    bindings are unaffected; a job in conflict at the key re-resolves on its next look.

    Args:
        tx: The write transaction.
        cache_key: The key.
        offering: If given, forget the entry only if it offers this step result. Use this to
            clear an entry that you read earlier, and not a different entry that replaced it.

    Returns:
        ``True`` if this function removed an entry.
    """
    condition = step_cache.c.cache_key == cache_key
    if offering is not None:
        condition = and_(condition, step_cache.c.step_result_id == offering)
    result = await tx.execute(step_cache.delete().where(condition))
    tx.wake(step_topic(cache_key))
    return result.rowcount > 0


type AdoptOutcome = Literal["adopted", "already", "equal_request", "running", "conflict"]
"""What :func:`adopt_step_result` did at a key. ``adopted``: the entry now offers the step result.
``already``: the entry offered it already. ``equal_request``: the entry offers a different
result of the same request. ``running``: an attempt runs at the key. ``conflict``: the entry
offers a result of a different request, and the call did not overwrite it."""


async def adopt_step_result(
    tx: Transaction,
    cache_key: str,
    step_result: ledger.StepResultRecord,
    *,
    overwrite_cache_conflicts: bool = False,
) -> AdoptOutcome:
    """Make the cache entry at a key offer a step result, if nothing live or equal is there.

    The function creates the entry if the key has none. It takes the entry over if its attempt
    failed or was abandoned. It does not change an entry whose attempt runs, or an entry that
    offers a result of the same request. It changes an entry that offers a result of a different
    request only if ``overwrite_cache_conflicts`` is ``True``. Each write is conditional on the
    entry as this function read it, so a concurrent change is not lost. In that case, the
    function reads the entry again and decides again.

    Args:
        tx: The write transaction.
        cache_key: The key.
        step_result: The step result to offer. Its request becomes the request of the entry.
        overwrite_cache_conflicts: If ``True``, replace an entry that offers a result of a
            different request.

    Returns:
        What the function did.
    """
    request_cid = step_result.request.cid
    while True:
        entry = await get_cache_entry(tx, cache_key)
        if entry is None:
            written = await tx.execute(
                tx.upsert(step_cache)
                .values(cache_key=cache_key, request_cid=request_cid, step_result_id=step_result.id)
                .on_conflict_do_nothing(index_elements=[step_cache.c.cache_key])
            )
        elif entry.step_result_id == step_result.id:
            return "already"
        elif entry.step_result_id is not None:
            if entry.request == step_result.request:
                return "equal_request"
            if not overwrite_cache_conflicts:
                return "conflict"
            written = await tx.execute(
                update(step_cache)
                .where(
                    step_cache.c.cache_key == cache_key,
                    step_cache.c.step_result_id == entry.step_result_id,
                )
                .values(
                    request_cid=request_cid, step_result_id=step_result.id, updated_at=func.now()
                )
            )
        else:
            assert entry.attempt_id is not None
            if (await get_attempt(tx, entry.attempt_id)).live:
                return "running"
            written = await tx.execute(
                update(step_cache)
                .where(
                    step_cache.c.cache_key == cache_key,
                    step_cache.c.attempt_id == attempt_uuid(entry.attempt_id),
                )
                .values(
                    request_cid=request_cid,
                    attempt_id=None,
                    step_result_id=step_result.id,
                    updated_at=func.now(),
                )
            )
        if written.rowcount > 0:
            tx.wake(step_topic(cache_key))
            return "adopted"


async def cache_conflicts(tx: Transaction, job_id: JobID) -> list[StepStateRecord]:
    """Get the keys where the requests of a job are in cache conflict, in key order.

    Args:
        tx: The transaction to read in.
        job_id: The job.

    Returns:
        The row of the step relation for each conflict. ``offered_step_result_id`` is set for a
        completed conflict. It is ``None`` for a running conflict.

    Raises:
        NotFoundError: If there is no such job.
    """
    await ledger.get_job(tx, job_id)
    return await StatusQueries([job_id]).conflict_records(tx)


# ---- status ----


async def status(tx: Transaction, job_id: JobID, invocation: InvocationRecord) -> TargetStatus:
    """What a run node of the job waiting on a child invocation should make of it: done with its
    result; pending while the job has an active job attempt and the child is running or due to
    run under it; otherwise stopped, with why.

    Every stop reported here is caused by something other than the asking attempt, so the asking
    attempt is interrupted by it, never failed.
    """
    if invocation.result is not None:
        return Done(invocation.result)
    job_attempt = await active_job_attempt(tx, job_id)
    if job_attempt is None:
        return Stopped("no active job attempt")
    running = await _running_attempt(tx, invocation.id)
    if running is not None:
        return Pending(running.id)
    owned = [
        a
        for a in await list_attempts(tx, invocation.id)
        if a.owner_job_attempt_id == job_attempt.id
    ]
    last = owned[-1] if owned else None
    if last is not None and last.state == "failed":
        return Stopped(f"child invocation {invocation.pathname!r} failed", error=last.error)
    if last is not None and last.state == "interrupted":
        return Stopped(f"child invocation {invocation.pathname!r} interrupted: {last.reason}")
    return Pending()  # due: the owning process will start it


async def step_status(tx: Transaction, job_id: JobID, cache_key: str) -> TargetStatus:
    """What a run node of the job waiting on a step at a cache key should make of it: done with
    the result its job's request is bound to; stopped by its own failure when the job's latest job
    attempt ran the step itself and that failed (its answer for the key until a resume, whatever
    other jobs do at the key afterwards); ready to be taken when an equal attempt has completed
    and the job has not bound to it yet; pending while the job has an active job attempt and the
    step is running or due to run; otherwise stopped, with why (a cache conflict with another
    request, no active job attempt, no request).

    The job's own failure, a result on offer, and a cache conflict are judged before whether a job
    attempt is active, so a stopped job still shows what stopped it, and the answer does not
    depend on what another job happens to be doing at the key. The judgement is the step relation
    of :class:`~fxtr.runner.postgres.status_queries.StatusQueries`, which the job's displayed
    status names too; its cache resolution part restates :func:`resolve_step` (see
    :mod:`fxtr.runner.postgres.status_queries`). Every stop reported here is caused by something
    other than the asking attempt, so the asking attempt is interrupted by it, never failed.
    """
    return (await step_statuses(tx, job_id, [cache_key]))[cache_key]


async def step_statuses(
    tx: Transaction, job_id: JobID, cache_keys: Collection[str]
) -> dict[str, TargetStatus]:
    """:func:`step_status` at each of the keys, by key, in one query: how the evaluator judges
    every step a pass waits on."""
    records = await StatusQueries([job_id]).step_records(tx, cache_keys)
    return {key: step_target_status(key, records.get(key)) for key in cache_keys}


def conflict_reason(cache_keys: Sequence[str]) -> str:
    """Make the reason for a run node that stopped on cache conflicts.

    Args:
        cache_keys: The keys of the conflicts. There must be at least one.

    Returns:
        For one key, a reason that names the key. For more keys, a reason that gives the number
        of keys and names the first key.
    """
    if len(cache_keys) == 1:
        return (
            f"cache conflict at {cache_keys[0]!r}: a different request owns the address; "
            "clear it to run this request there"
        )
    return (
        f"cache conflicts at {len(cache_keys)} addresses, such as {cache_keys[0]!r}: different"
        " requests own them; clear them to run these requests there"
    )


def step_target_status(cache_key: str, state: StepStateRecord | None) -> TargetStatus:
    """The step relation's row at a key (``None``: the job made no request there) as the
    :class:`TargetStatus` of :func:`step_status`."""
    if state is None:
        return Stopped(f"step at {cache_key!r} was not requested")
    match state.target_state:
        case "done":
            result = bound_array(state.result_cid)
            assert result is not None
            return Done(result)
        case "failed":
            assert state.failed_attempt_id is not None
            error = state.failure_error
            message = "" if error is None else f": {error.message}"
            return Stopped(
                f"step attempt at {cache_key!r} failed{message}",
                error=error,
                attempt_id=state.failed_attempt_id,
            )
        case "ready":
            assert state.offered_step_result_id is not None
            return Ready(state.offered_step_result_id)
        case "conflict":
            return Stopped(conflict_reason([cache_key]), cache_conflict=True)
        case "stopped":
            return Stopped("no active job attempt")
        case "running":
            assert state.current_attempt_id is not None
            return Pending(state.current_attempt_id)
        case "queued":
            return Pending()


async def _latest_job_attempt(tx: Transaction, job_id: JobID) -> JobAttemptRecord | None:
    """The job's most recent job attempt, active or settled."""
    row = (
        await tx.execute(
            _job_attempt_query()
            .where(job_attempts.c.job_id == job_id)
            .order_by(job_attempt_provenance.c.created_at.desc(), job_attempts.c.id.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else await _job_attempt(tx, row)


async def invocation_state(
    tx: Transaction, invocation: InvocationRecord
) -> tuple[InvocationState, AttemptRecord | None]:
    """Derive an invocation's status for display: ``completed`` if finished; ``running`` if an
    attempt of it runs; ``queued`` if the job's active job attempt is due to run it; else by
    the latest attempt, ``failed`` or ``interrupted`` if that attempt was, else ``stopped``.

    ``stopped`` is the residual case: incomplete and idle, with no attempt under the current
    episode to explain why (never tried, the last attempt abandoned, or no active job attempt).
    ``failed`` and ``interrupted`` are the same idle condition with an informative last attempt;
    all three are resumable.

    Returns:
        The state, and the attempt it was read from (the running one, or the latest), if any.
    """
    projection = StatusQueries([invocation.job_id], invocation_id=invocation.id)
    row = (
        await tx.execute(
            select(projection.invocations).where(projection.invocations.c.id == invocation.id)
        )
    ).one()
    attempt = (
        None
        if row.status_attempt_id is None
        else await get_attempt(tx, AttemptID(str(row.status_attempt_id)))
    )
    return row.display_state, attempt


# ---- resume ----


async def resume(
    tx: Transaction,
    job_id: JobID,
    *,
    owner: str,
    code_version: BoundID[CodeVersion],
    take_over_after: timedelta | None = None,
    overwrite_cache_conflicts: bool = False,
) -> JobAttemptRecord | AlreadyFinished:
    """Take the job for ``owner``, asking for everything unfinished in it to be finished. This is
    the one way a process comes to hold a job: a launch calls it for a job it has just created, and
    a resume calls it for a job that stopped.

    The call ends in one of three ways.

    1. There is nothing to resume, because the job's root already has its result. The call
       returns :class:`AlreadyFinished` and creates nothing.
    2. Another process holds the job: its active job attempt has a fresh lease, meaning it has
       heartbeaten within ``take_over_after``. The call raises :class:`JobRunningElsewhereError`.
       The lease is judged by its heartbeat alone, not by who holds it, so the process holding the
       job is refused like any other if it asks again. With no ``take_over_after`` an active job
       attempt is never taken over, however long it has been quiet, so a call without a threshold
       is refused by every active job attempt.
    3. A new job attempt is created for ``owner``, recording ``code_version``, and returned. This
       happens when the job has no active job attempt, or when its active job attempt is stale (no
       heartbeat for ``take_over_after``): the stale one is then *taken over* first, which evicts
       it, abandoning whatever its process was still running, and settles it ``stopped``. A
       takeover is conditional on the heartbeat still being stale when the eviction runs, so if the
       presumed-dead process heartbeats just in time the call raises
       :class:`JobRunningElsewhereError` after all.

    Before deciding, the call settles the job's active job attempt if its work is finished or
    exhausted (:func:`settle`), since its process may have died between closing its last attempt
    and settling; that is how a job attempt that was left ``active`` by a crash gets out of the
    way without a takeover. Nothing is scheduled here: what the returned job attempt is due to
    start follows from :func:`pending_work`, and running it is the job runner's business.

    The job's own row is taken first, so two resumes of one job serialize and the second sees the
    first's job attempt.

    Args:
        job_id: The job.
        owner: The string the new job attempt's lease is held under, and that its heartbeats
            refresh it under. A label naming the process (and, if the process chooses, the
            episode); it decides nothing in this call.
        code_version: The code the process runs, which the new job attempt records; it must
            already be committed to the entity store (this transaction only references it).
        take_over_after: How long the active job attempt may have gone without a heartbeat before
            it counts as stale and is taken over. ``None`` never takes over.
        overwrite_cache_conflicts: If ``True``, the new job attempt overwrites cache conflicts
            with completed results (see :func:`create_job_attempt`).

    Returns:
        The job attempt now holding the job, owned by ``owner``; or :class:`AlreadyFinished` if
        the root has its result (the caller decides whether that is an error; raising here would
        roll the settle back with it).

    Raises:
        NotFoundError: If there is no such job.
        JobRunningElsewhereError: If the job's active job attempt has a fresh lease.
    """
    await _lock_job(tx, job_id)
    job = await ledger.get_job(tx, job_id)
    await settle(tx, job_id)
    root = await ledger.find_invocation(tx, job_id, job.root)
    if root.finished:
        return AlreadyFinished(root, await _latest_job_attempt(tx, job_id))
    job_attempt = await active_job_attempt(tx, job_id)
    if job_attempt is not None:
        if take_over_after is None or job_attempt.fresh(take_over_after):
            raise JobRunningElsewhereError(
                f"Job {job_id} is being run by {job_attempt.owner!r} (job attempt {job_attempt.id})"
            )
        swept = await evict(
            tx, job_attempt, f"taken over by {owner}", only_if_stale=take_over_after
        )
        if swept is None:
            raise JobRunningElsewhereError(
                f"Job {job_id} is being run by {job_attempt.owner!r} (job attempt"
                f" {job_attempt.id}; its heartbeat arrived as this takeover ran)"
            )
    return await create_job_attempt(
        tx,
        job_id,
        owner=owner,
        code_version=code_version,
        overwrite_cache_conflicts=overwrite_cache_conflicts,
    )


# ---- readers ----


async def get_job_attempt(tx: Transaction, job_attempt_id: JobAttemptID) -> JobAttemptRecord:
    """The job attempt.

    Raises:
        NotFoundError: If there is no such job attempt.
    """
    row = (
        await tx.execute(_job_attempt_query().where(job_attempts.c.id == job_attempt_id))
    ).first()
    if row is None:
        raise NotFoundError(f"No job attempt {job_attempt_id}")
    return await _job_attempt(tx, row)


async def list_job_attempts(tx: Transaction, job_id: JobID) -> list[JobAttemptRecord]:
    """A job's job attempts, oldest first."""
    rows = (
        await tx.execute(
            _job_attempt_query()
            .where(job_attempts.c.job_id == job_id)
            .order_by(job_attempt_provenance.c.created_at, job_attempts.c.id)
        )
    ).all()
    return [await _job_attempt(tx, row) for row in rows]


async def get_cache_entry(tx: Transaction, cache_key: str) -> CacheEntryRecord | None:
    """The cache entry at a key, or ``None`` if the key is free."""
    row = (await tx.execute(select(step_cache).where(step_cache.c.cache_key == cache_key))).first()
    return None if row is None else _cache_entry(row)


async def get_cache_entries(
    tx: Transaction, cache_keys: Collection[str]
) -> dict[str, CacheEntryRecord]:
    """:func:`get_cache_entry` for many keys, by key; a free key is absent."""
    found: dict[str, CacheEntryRecord] = {}
    for batch in itertools.batched(cache_keys, ledger.BATCH_SIZE):
        rows = await tx.execute(select(step_cache).where(step_cache.c.cache_key.in_(batch)))
        found.update((row.cache_key, _cache_entry(row)) for row in rows)
    return found


async def list_cache_entries(tx: Transaction) -> list[CacheEntryRecord]:
    """Every step cache entry, oldest first."""
    rows = (
        await tx.execute(
            select(step_cache).order_by(step_cache.c.created_at, step_cache.c.cache_key)
        )
    ).all()
    return [_cache_entry(row) for row in rows]


async def select_cache_entries(
    tx: Transaction,
    *,
    prefix: str | None = None,
    keys: Collection[str] | None = None,
    step: str | None = None,
    state: CacheEntryState | None = None,
) -> list[CacheEntry]:
    """Select cache entries in one query, oldest first, with the step and the state of each.

    Each filter that is not ``None`` narrows the selection. An entry is ``completed`` if it offers
    a step result. Otherwise its state comes from its current attempt: ``running``, ``failed``, or
    ``abandoned``.

    Args:
        prefix: Select only the keys at or under this prefix (see
            :func:`~fxtr.runner.cache_addresses.under_prefix`). The empty prefix selects all keys.
            The prefix can be a pattern, in which a mapped value is ``*`` (see
            :meth:`~fxtr.runner.cache_addresses.AddressPattern.selects`).
        keys: Select only these keys.
        step: Select only the entries whose request calls the step of this name.
        state: Select only the entries in this state.

    Returns:
        The selected entries.

    Raises:
        PatternError: If ``prefix`` has a ``*`` in a place other than a value.
    """
    pattern = parse_pattern(prefix) if prefix else None
    entry_state = case(
        (step_cache.c.step_result_id.is_not(None), "completed"),
        (attempts.c.state == "running", "running"),
        (attempts.c.state == "failed", "failed"),
        else_="abandoned",
    ).label("entry_state")
    query = (
        select(step_cache.c.cache_key, step_requests.c.step_name, entry_state)
        .select_from(
            step_cache.join(
                step_requests, step_requests.c.request_cid == step_cache.c.request_cid
            ).outerjoin(attempts, attempts.c.id == step_cache.c.attempt_id)
        )
        .order_by(step_cache.c.created_at, step_cache.c.cache_key)
    )
    key = step_cache.c.cache_key
    if pattern is not None and pattern.has_wildcards:
        # SQL narrows to the text before the first group; the pattern decides in Python.
        query = query.where(key.startswith(pattern.literal_prefix, autoescape=True))
    elif prefix:
        query = query.where(
            or_(
                key == prefix,
                key.startswith(prefix + "/", autoescape=True),
                key.startswith(prefix + "[", autoescape=True),
            )
        )
    if keys is not None:
        query = query.where(step_cache.c.cache_key.in_(list(keys)))
    if step is not None:
        query = query.where(step_requests.c.step_name == step)
    if state is not None:
        query = query.where(entry_state == state)
    rows = (await tx.execute(query)).all()
    if pattern is not None and pattern.has_wildcards:
        rows = [row for row in rows if pattern.selects(row.cache_key)]
    return [CacheEntry(row.cache_key, row.step_name, row.entry_state) for row in rows]


async def get_attempt(tx: Transaction, attempt_id: AttemptID) -> AttemptRecord:
    """The attempt.

    Raises:
        NotFoundError: If there is no such attempt.
    """
    found = await find_attempt(tx, attempt_id)
    if found is None:
        raise NotFoundError(f"No attempt {attempt_id}")
    return found


async def find_attempt(tx: Transaction, attempt_id: AttemptID) -> AttemptRecord | None:
    """The attempt, or ``None`` if this database has no such attempt (as for the producer of a step
    result that came from elsewhere)."""
    row = (
        await tx.execute(_attempt_query().where(attempts.c.id == attempt_uuid(attempt_id)))
    ).first()
    return None if row is None else _attempt(row)


async def find_attempts(
    tx: Transaction, attempt_ids: Collection[AttemptID]
) -> dict[AttemptID, AttemptRecord]:
    """:func:`find_attempt` for many, by id; an attempt this database does not have is absent."""
    found: dict[AttemptID, AttemptRecord] = {}
    for batch in itertools.batched(attempt_ids, ledger.BATCH_SIZE):
        rows = await tx.execute(
            _attempt_query().where(attempts.c.id.in_([attempt_uuid(a) for a in batch]))
        )
        found.update((record.id, record) for record in map(_attempt, rows))
    return found


async def get_attempts(
    tx: Transaction, attempt_ids: Collection[AttemptID]
) -> dict[AttemptID, AttemptRecord]:
    """:func:`get_attempt` for many, by id.

    Raises:
        NotFoundError: If any of them does not exist.
    """
    found = await find_attempts(tx, attempt_ids)
    missing = set(attempt_ids) - found.keys()
    if missing:
        raise NotFoundError(f"No attempts {sorted(missing)}")
    return found


async def list_attempts(tx: Transaction, invocation_id: InvocationID) -> list[AttemptRecord]:
    """An invocation's attempts, oldest first."""
    rows = (
        await tx.execute(
            _attempt_query()
            .where(attempts.c.invocation_id == invocation_id)
            .order_by(attempts.c.started_at)
        )
    ).all()
    return [_attempt(row) for row in rows]


async def list_owned_attempts(tx: Transaction, job_attempt_id: JobAttemptID) -> list[AttemptRecord]:
    """Every attempt a job attempt owns, oldest first."""
    rows = (
        await tx.execute(
            _attempt_query()
            .where(attempts.c.owner_job_attempt_id == job_attempt_id)
            .order_by(attempts.c.started_at)
        )
    ).all()
    return [_attempt(row) for row in rows]


# ---- row decoding ----


def _error_json(error: AttemptError) -> dict[str, str]:
    return dataclasses.asdict(error)


def _error(value: Any) -> AttemptError | None:
    return None if value is None else AttemptError(**value)


def _job_attempt_query() -> Any:
    """The job attempt columns with its fact's (which code, when created), the database's clock,
    so freshness is judged against the same clock the heartbeats are written with, and the job
    attempt's position among its job's (``seq``), counted in creation order over the job's job
    attempt facts (an imported history included)."""
    fact = job_attempt_provenance
    earlier = fact.alias("earlier")
    seq = (
        select(func.count())
        .where(
            earlier.c.job_id == fact.c.job_id,
            tuple_(earlier.c.created_at, earlier.c.id) <= tuple_(fact.c.created_at, fact.c.id),
        )
        .correlate(fact)
        .scalar_subquery()
    )
    return select(
        job_attempts,
        fact.c.code_version_cid,
        fact.c.created_at,
        seq.label("seq"),
        func.now().label("read_at"),
    ).select_from(job_attempts.join(fact, fact.c.id == job_attempts.c.id))


async def _job_attempt(tx: Transaction, row: Row[Any]) -> JobAttemptRecord:
    return JobAttemptRecord(
        id=JobAttemptID(row.id),
        job_id=JobID(row.job_id),
        seq=row.seq,
        state=row.state,
        reason=row.reason,
        owner=row.owner,
        code_version=EntityID(cid=bytes(row.code_version_cid)).bind_type(CodeVersion),
        heartbeat_at=row.heartbeat_at,
        evicted_at=row.evicted_at,
        evicted_reason=row.evicted_reason,
        created_at=row.created_at,
        settled_at=row.settled_at,
        read_at=row.read_at,
        overwrite_cache_conflicts=row.overwrite_cache_conflicts,
    )


def _cache_entry(row: Row[Any]) -> CacheEntryRecord:
    return CacheEntryRecord(
        cache_key=row.cache_key,
        request=EntityID(cid=bytes(row.request_cid)),
        attempt_id=None if row.attempt_id is None else AttemptID(str(row.attempt_id)),
        step_result_id=None if row.step_result_id is None else StepResultID(row.step_result_id),
    )


def _attempt_query() -> Any:
    """Attempt rows, with a completed step attempt's result read from its step result (the ledger
    row with the attempt's id)."""
    return select(attempts, step_results.c.result_cid).outerjoin(
        step_results, step_results.c.id == attempts.c.id
    )


def _attempt(row: Row[Any]) -> AttemptRecord:
    return AttemptRecord(
        id=AttemptID(str(row.id)),
        kind=row.kind,
        invocation_id=None if row.invocation_id is None else InvocationID(row.invocation_id),
        cache_key=row.cache_key,
        request=identity_from_bytes(row.request_cid),
        owner_job_attempt_id=JobAttemptID(row.owner_job_attempt_id),
        state=row.state,
        result=bound_array(row.result_cid),
        error=_error(row.error),
        reason=row.reason,
        stopped_on=None
        if row.stopped_on is None
        else tuple(EntityID(cid=cid_from_str(cid)) for cid in row.stopped_on),
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


__all__ = [
    "ActiveStepAttempt",
    "AdoptOutcome",
    "AlreadyFinished",
    "AttemptKind",
    "AttemptRecord",
    "AttemptState",
    "Bound",
    "CacheEntryRecord",
    "ClosedState",
    "Completed",
    "ConflictingEntry",
    "Done",
    "InvocationState",
    "JobAttemptID",
    "JobAttemptRecord",
    "JobAttemptState",
    "JobRunningElsewhereError",
    "NotCancellableError",
    "NotResumableError",
    "Pending",
    "Ready",
    "RunIt",
    "Settlement",
    "StaleAttemptError",
    "StartInvocation",
    "StartStep",
    "Started",
    "StartedStep",
    "StepResolution",
    "Stopped",
    "TargetStatus",
    "WorkItem",
    "active_job_attempt",
    "adopt_step_result",
    "cache_conflicts",
    "cancel",
    "clear_cache_entry",
    "close_attempt",
    "close_step_attempt",
    "complete_step",
    "conflict_reason",
    "create_job_attempt",
    "evict",
    "find_attempt",
    "get_attempt",
    "get_cache_entry",
    "get_job_attempt",
    "guard",
    "guard_step",
    "heartbeat",
    "invocation_state",
    "job_topic",
    "list_attempts",
    "list_cache_entries",
    "list_job_attempts",
    "list_owned_attempts",
    "lock_job_attempt",
    "materialize_result",
    "pending_work",
    "resolve_step",
    "resume",
    "select_cache_entries",
    "start_attempt",
    "status",
    "settle",
    "step_status",
    "step_statuses",
    "step_topic",
    "take_step_result",
]
