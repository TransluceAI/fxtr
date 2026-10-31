"""Status as relations: what each target of a job is, derived in SQL for many targets at once.

Three questions are asked about a job's targets, and they have different answers:

* **Cache resolution**, for a job's request at a step's cache key: is it bound, is an equal
  request's result on offer, is an equal attempt running, is the key in conflict with a different
  request, or may the job run it. :func:`~fxtr.runner.postgres.execution.resolve_step` answers it
  for the write paths (expanding a run node into step requests, starting a step attempt, taking a
  result), each inside its own write transaction, since they need its typed answer: the attempt a
  new one replaces, the result to bind.
* **Execution policy**, for a run node waiting on a target: done, ready to take, pending, or
  stopped (``target_state`` here, hydrated by
  :func:`~fxtr.runner.postgres.execution.step_statuses`). It layers the job's own facts over the
  cache resolution: its latest job attempt's own failure at the key wins over anything another job
  does there, and without an active job attempt nothing is pending.
* **Display**, what a person is shown (``display_state``): the policy's judgement under other
  names, where a result on offer shows as running while the job can take it and stopped while it
  cannot, and a failure without a recorded error shows as stopped.

The step relation states the cache resolution rules relationally, so they exist twice, here and
in ``resolve_step``, and must change together. The correspondence is: ``done`` is ``Bound``,
``ready`` is ``Completed`` (the entry offers a step result), ``conflict`` is
``ConflictingEntry``, ``running`` is ``Pending`` with its attempt, and ``queued`` is ``RunIt`` under
an active job attempt that has not failed at the key; ``failed`` and ``stopped`` overlay any
unbound resolution. A different request's step result on offer is ``conflict``, unless the active
job attempt overwrites cache conflicts: then it is ``queued``, as ``RunIt`` with ``overwrites``.
``assert_step_policy_agrees`` in ``fxtr/tests/postgres_testing.py`` checks it wherever the
execution tests read a step's status.

What a job attempt is due to start is read from both relations:
:func:`~fxtr.runner.postgres.execution.pending_work` lists their ``queued`` rows, so the queue a
runner derives is what the job's status shows as queued. What the evaluator makes of a child
invocation is :func:`~fxtr.runner.postgres.execution.status`, which asks whether the child is
pending under the active job attempt rather than how to describe it.

Relations project compact facts only (no entity payloads, events, or logs), resolve the latest and
active job attempts relationally in the caller's transaction, and can be filtered or aggregated
before anything is hydrated. Each half is built on first use.

**Planning without the job IDs.** The step relation reaches its jobs only through the join to
``status_jobs``, which is ``MATERIALIZED``, so the planner never sees a job ID as a constant. With a
constant, Postgres estimates a job's rows from its statistics, and a new job missing from them is
estimated at one row. A scan of the ``(job_id, cache_key)`` primary keys of ``job_step_requests``
and ``job_step_bindings`` by ``job_id`` alone then costs the same as a lookup by both columns, and
the planner may pick it, matching ``cache_key`` in a join filter: a run node's spawns times the
job's requests times its bindings, minutes for a few thousand spawns. Through the join, the key can
be used only by a lookup parameterized by the outer row, which takes both columns, so the cost
stays linear in the rows read whatever the estimates. Without ``MATERIALIZED``, a query reading
only the step relation would inline ``status_jobs`` and turn the job ID back into a constant.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Row, and_, any_, case, cast, func, or_, select, true
from sqlalchemy.dialects.postgresql import ARRAY

from fxtr.runner.session import AttemptError, AttemptID

from .database import Transaction
from .ledger import InvocationID, JobID, StepResultID
from .schema import (
    attempts,
    job_attempt_provenance,
    job_attempts,
    job_step_bindings,
    job_step_requests,
    jobs,
    spawns,
    step_cache,
    step_requests,
    step_results,
    workflow_invocations,
)

type StepState = Literal["queued", "running", "completed", "failed", "conflict", "stopped"]
"""A requested step's state for display: ``completed`` once the job's request is bound;
``failed`` when its latest job attempt ran a failed step attempt with a recorded error, even if
the job attempt has settled; ``conflict`` when a different request owns the key through a running
attempt or a step result on offer. Otherwise, while the job's latest attempt is active,
``running`` means an equal attempt is computing the result or a step result is ready to take, and
``queued`` means the step is due to run. With no active job attempt, or a failure without a
recorded error, it is ``stopped``.
A binding takes precedence over failure, and the job's own failure takes precedence over the
cache entry's state."""

type TargetState = Literal["done", "failed", "ready", "conflict", "stopped", "running", "queued"]


@dataclass(frozen=True)
class StepStateRecord:
    """One requested step's row of the step relation.

    Attributes:
        offered_step_result_id: The step result the cache entry offers, for ``ready``.
        failure_error: The error of the job's own failed attempt at the key, for ``failed``.
        entry_step_name: The step of the request that owns the cache entry at the key. ``None``
            if the key has no entry. For ``conflict``, this is the step whose result or attempt
            blocks the request.
        display_attempt_id: The attempt the display answers with: the one that produced the bound
            (``done``) or offered (``ready``) step result, the failed one, or the current one;
            ``None`` when there is none, or when the producing attempt of a step result is not in
            this database (it came from elsewhere).
    """

    cache_key: str
    target_state: TargetState
    display_state: StepState
    result_cid: bytes | None
    offered_step_result_id: StepResultID | None
    current_attempt_id: AttemptID | None
    failed_attempt_id: AttemptID | None
    failure_error: AttemptError | None
    display_attempt_id: AttemptID | None
    entry_step_name: str | None = None


def _attempt_id(value: UUID | None) -> AttemptID | None:
    return None if value is None else AttemptID(str(value))


class StatusQueries:
    """The status relations of some jobs: ``invocations`` (every invocation of them, or with
    ``invocation_id`` only that one) and ``steps`` (every step they requested)."""

    def __init__(self, job_ids: Sequence[JobID], *, invocation_id: InvocationID | None = None):
        self._job_ids = job_ids
        self._invocation_id = invocation_id
        latest = (
            select(job_attempts.c.id, job_attempts.c.state)
            .join(job_attempt_provenance, job_attempt_provenance.c.id == job_attempts.c.id)
            .where(job_attempts.c.job_id == jobs.c.id)
            .order_by(job_attempt_provenance.c.created_at.desc(), job_attempts.c.id.desc())
            .limit(1)
            .lateral("latest_job_attempt")
        )
        active = job_attempts.alias("active_job_attempt")
        self.jobs = (
            select(
                jobs,
                latest.c.id.label("latest_id"),
                latest.c.state.label("latest_state"),
                active.c.id.label("active_id"),
                active.c.overwrite_cache_conflicts.label("active_overwrites"),
            )
            .select_from(
                jobs.outerjoin(latest, true()).outerjoin(
                    active, and_(active.c.job_id == jobs.c.id, active.c.state == "active")
                )
            )
            .where(jobs.c.id.in_(job_ids))
            .cte("status_jobs")
            # See "Planning without the job IDs" in the module docstring.
            .prefix_with("MATERIALIZED")
        )

    @cached_property
    def invocations(self) -> Any:
        job_ids, invocation_id = self._job_ids, self._invocation_id
        context = self.jobs.c
        inv = workflow_invocations
        a = attempts
        invocation_rows = inv.join(self.jobs, inv.c.job_id == context.id)
        own = select(a.c.id).where(a.c.invocation_id == inv.c.id).correlate(inv, self.jobs)
        last_state = (
            select(a.c.state)
            .where(a.c.invocation_id == inv.c.id)
            .order_by(a.c.started_at.desc(), a.c.id.desc())
            .limit(1)
            .correlate(inv)
            .scalar_subquery()
        )
        # Due under the active job attempt, which tries each of its job's invocations once.
        due = and_(
            context.active_id.is_not(None),
            ~own.where(
                a.c.owner_job_attempt_id == context.active_id,
                a.c.state != "abandoned",
            ).exists(),
        )
        state = case(
            (inv.c.result_cid.is_not(None), "completed"),
            (own.where(a.c.state == "running").exists(), "running"),
            (due, "queued"),
            (last_state == "failed", "failed"),
            (last_state == "interrupted", "interrupted"),
            else_="stopped",
        )
        latest_id = (
            select(a.c.id)
            .where(a.c.invocation_id == inv.c.id)
            .order_by(a.c.started_at.desc(), a.c.id.desc())
            .limit(1)
            .correlate(inv)
            .scalar_subquery()
        )
        running_id = (
            select(a.c.id)
            .where(a.c.invocation_id == inv.c.id, a.c.state == "running")
            .limit(1)
            .correlate(inv)
            .scalar_subquery()
        )
        status_attempt = case(
            (inv.c.result_cid.is_not(None), None), else_=func.coalesce(running_id, latest_id)
        )
        return (
            select(
                inv,
                state.label("display_state"),
                status_attempt.label("status_attempt_id"),
                func.coalesce(running_id, latest_id).label("activity_attempt_id"),
            )
            .select_from(invocation_rows)
            .where(inv.c.job_id.in_(job_ids))
            .where(true() if invocation_id is None else inv.c.id == invocation_id)
            .cte("invocation_states")
        )

    @cached_property
    def steps(self) -> Any:
        context = self.jobs.c
        a = attempts
        req, binding, cache = job_step_requests, job_step_bindings, step_cache
        bound = step_results.alias("bound_step_result")
        # The attempts that produced the bound and the offered step results (a step result has its
        # producing attempt's id), when this database ran them.
        bound_producer = a.alias("bound_producer")
        offered_producer = a.alias("offered_producer")
        current = a.alias("current_step_attempt")
        entry_request = step_requests.alias("entry_request")
        failed = a.alias("failed_step_attempt")
        failed_id = (
            select(a.c.id)
            .where(
                a.c.owner_job_attempt_id == context.latest_id,
                a.c.cache_key == req.c.cache_key,
                a.c.state == "failed",
            )
            .order_by(a.c.started_at.desc(), a.c.id.desc())
            .limit(1)
            .correlate(req, self.jobs)
            .scalar_subquery()
        )
        active_latest = func.coalesce(context.latest_state == "active", False)
        overwrites = func.coalesce(context.active_overwrites, False)
        same = cache.c.request_cid == req.c.request_cid
        offered = cache.c.step_result_id.is_not(None)
        # The execution policy's judgement (see the module docstring), which displays name.
        target = case(
            (binding.c.step_result_id.is_not(None), "done"),
            (failed.c.id.is_not(None), "failed"),
            (and_(same, offered), "ready"),
            (
                and_(~same, or_(and_(offered, ~overwrites), current.c.state == "running")),
                "conflict",
            ),
            (~active_latest, "stopped"),
            (and_(same, current.c.state == "running"), "running"),
            else_="queued",
        )
        facts = (
            select(
                req.c.job_id,
                req.c.cache_key,
                req.c.requested_at,
                active_latest.label("active_latest"),
                bound.c.result_cid,
                binding.c.step_result_id.label("bound_step_result_id"),
                cache.c.step_result_id.label("offered_step_result_id"),
                bound_producer.c.id.label("bound_attempt_id"),
                offered_producer.c.id.label("offered_attempt_id"),
                current.c.id.label("current_attempt_id"),
                failed.c.id.label("failed_attempt_id"),
                failed.c.error.label("failure_error"),
                entry_request.c.step_name.label("entry_step_name"),
                target.label("target_state"),
            )
            .select_from(
                req.join(self.jobs, req.c.job_id == context.id)
                .outerjoin(
                    binding,
                    and_(req.c.job_id == binding.c.job_id, req.c.cache_key == binding.c.cache_key),
                )
                .outerjoin(bound, bound.c.id == binding.c.step_result_id)
                .outerjoin(bound_producer, bound_producer.c.id == binding.c.step_result_id)
                .outerjoin(cache, req.c.cache_key == cache.c.cache_key)
                .outerjoin(offered_producer, offered_producer.c.id == cache.c.step_result_id)
                .outerjoin(current, cache.c.attempt_id == current.c.id)
                .outerjoin(entry_request, entry_request.c.request_cid == cache.c.request_cid)
                .outerjoin(failed, failed.c.id == failed_id)
            )
            # No job_id filter: the join to status_jobs restricts the jobs. See "Planning without
            # the job IDs" in the module docstring.
            .cte("step_facts")
        )
        f = facts.c
        display = case(
            (f.target_state == "done", "completed"),
            (
                f.target_state == "failed",
                case((f.failure_error.is_not(None), "failed"), else_="stopped"),
            ),
            (f.target_state == "ready", case((f.active_latest, "running"), else_="stopped")),
            else_=f.target_state,
        )
        display_attempt = case(
            (f.target_state == "done", f.bound_attempt_id),
            (and_(f.target_state == "failed", f.failure_error.is_not(None)), f.failed_attempt_id),
            (f.target_state.in_(["conflict", "queued"]), None),
            (f.target_state == "ready", f.offered_attempt_id),
            else_=f.current_attempt_id,
        )
        return select(
            facts, display.label("display_state"), display_attempt.label("display_attempt_id")
        ).cte("step_states")

    async def invocation_records(
        self, tx: Transaction, invocation_ids: Collection[InvocationID]
    ) -> Sequence[Row[Any]]:
        """Selected invocation rows with their answering attempt's error and reason, in one
        query. Only invocations within this object's configured relation are returned."""
        if not invocation_ids:
            return []
        relation = self.invocations
        ids = cast(list(invocation_ids), ARRAY(relation.c.id.type))
        return (
            await tx.execute(
                select(relation, attempts.c.error, attempts.c.reason)
                .outerjoin(attempts, relation.c.status_attempt_id == attempts.c.id)
                .where(relation.c.id == any_(ids))
            )
        ).all()

    async def step_records(
        self, tx: Transaction, cache_keys: Collection[str]
    ) -> dict[str, StepStateRecord]:
        """The step relation's rows at the keys, in one query, by key; a key the jobs did not
        request is absent. For one job: rows of several jobs at one key would collide."""
        assert len(self._job_ids) == 1, "step rows are keyed by cache key within one job"
        if not cache_keys:
            return {}
        relation = self.steps
        keys = cast(list(cache_keys), ARRAY(relation.c.cache_key.type))
        rows = (await tx.execute(select(relation).where(relation.c.cache_key == any_(keys)))).all()
        return {row.cache_key: self.step_record(row) for row in rows}

    async def named_step_records(self, tx: Transaction) -> list[tuple[StepStateRecord, str]]:
        """Every row of the step relation with the name of the step it runs, in request order, in
        one query: what a job's status shows for each of its steps. For one job, as
        :meth:`step_records`."""
        assert len(self._job_ids) == 1, "step rows are keyed by cache key within one job"
        relation = self.steps
        req = job_step_requests
        rows = (
            await tx.execute(
                select(relation, step_requests.c.step_name)
                .join(
                    req,
                    and_(
                        req.c.job_id == relation.c.job_id, req.c.cache_key == relation.c.cache_key
                    ),
                )
                .join(step_requests, step_requests.c.request_cid == req.c.request_cid)
                .order_by(relation.c.requested_at, relation.c.cache_key)
            )
        ).all()
        return [(self.step_record(row), row.step_name) for row in rows]

    async def conflict_records(self, tx: Transaction) -> list[StepStateRecord]:
        """Get the rows of the step relation in ``conflict``, in key order, in one query.

        Use this for one job only, as for :meth:`step_records`.

        Args:
            tx: The transaction to read in.

        Returns:
            One row for each key where the request of the job is in cache conflict.
        """
        assert len(self._job_ids) == 1, "step rows are keyed by cache key within one job"
        relation = self.steps
        rows = (
            await tx.execute(
                select(relation)
                .where(relation.c.target_state == "conflict")
                .order_by(relation.c.cache_key)
            )
        ).all()
        return [self.step_record(row) for row in rows]

    @staticmethod
    def step_record(row: Any) -> StepStateRecord:
        return StepStateRecord(
            cache_key=row.cache_key,
            target_state=row.target_state,
            display_state=row.display_state,
            result_cid=row.result_cid,
            offered_step_result_id=None
            if row.offered_step_result_id is None
            else StepResultID(row.offered_step_result_id),
            current_attempt_id=_attempt_id(row.current_attempt_id),
            failed_attempt_id=_attempt_id(row.failed_attempt_id),
            failure_error=None if row.failure_error is None else AttemptError(**row.failure_error),
            display_attempt_id=_attempt_id(row.display_attempt_id),
            entry_step_name=row.entry_step_name,
        )

    def spawn_query(self, invocation_id: Any, node_id: bytes | None = None):
        """One row per spawn of the invocation (of one node, given ``node_id``): its key, its
        child invocation or step, result, display ``state``, and ``attempt_id``, the attempt whose
        timing and error describe it: a child's running or latest attempt (its latest even once
        it completed), a step's displayed attempt (none while in conflict or queued)."""
        child, step = self.invocations, self.steps
        parent = workflow_invocations.alias("spawn_parent")
        query = (
            select(
                spawns.c.node_identity,
                spawns.c.key,
                child.c.pathname.label("child_pathname"),
                spawns.c.step_cache_key,
                func.coalesce(child.c.result_cid, step.c.result_cid).label("result_cid"),
                func.coalesce(child.c.display_state, step.c.display_state).label("state"),
                func.coalesce(child.c.activity_attempt_id, step.c.display_attempt_id).label(
                    "attempt_id"
                ),
            )
            .select_from(
                spawns.join(parent, spawns.c.invocation_id == parent.c.id)
                .outerjoin(child, spawns.c.child_invocation_id == child.c.id)
                .outerjoin(
                    step,
                    and_(
                        spawns.c.step_cache_key == step.c.cache_key,
                        step.c.job_id == parent.c.job_id,
                    ),
                )
            )
            .where(spawns.c.invocation_id == invocation_id)
        )
        if node_id is not None:
            query = query.where(spawns.c.node_identity == node_id)
        return query
