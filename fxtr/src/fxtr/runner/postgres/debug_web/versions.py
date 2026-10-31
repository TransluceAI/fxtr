"""Change signals for the debug viewer: a cheap version string per invocation, for polling.

:func:`invocation_version` returns an opaque string that changes whenever the ``invocation``
field (:meth:`~fxtr.runner.postgres.jobs.Jobs.describe_invocation`) would return something
different, spawn states included, so a polling client refetches an invocation only when it moved.
It is a change signal only, never a source of state: nothing reads meaning out of it.

Detailed records, overviews, and individual runs share this ledger-based policy. Run revisions
restrict target facts to one run node, so another run's progress does not invalidate its pages.
Reads within an overview use its transaction, keeping the revision and data in one snapshot.

**Counts, not timestamps.** A timestamp watermark (the latest ``created_at``/``finished_at``) can
miss a change: ``now()`` is a transaction's *start* time, so a transaction committing late can add
a row older than one a client already saw. Counts cannot. Every counted set only grows, and every
per-state tally is over rows whose state only moves forward, so each tally vector is strictly
monotone (a transition moves one row to a later state; the same vector twice means nothing moved).
The one exception is the step cache, whose entries at a key are replaced and deleted; for those the
version carries a digest of the current entries' attempts instead of a count.
"""

from __future__ import annotations

import typing
from typing import Any

from sqlalchemy import ColumnElement, Select, Text, func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by

from fxtr.runner.postgres.database import Database, Transaction
from fxtr.runner.postgres.execution import AttemptState, JobAttemptState
from fxtr.runner.postgres.ledger import InvocationID, JobID
from fxtr.runner.postgres.schema import (
    attempt_events,
    attempt_logs,
    attempts,
    job_attempts,
    job_step_bindings,
    jobs,
    nodes,
    spawns,
    step_cache,
    workflow_invocations,
)

_ATTEMPT_STATES: tuple[str, ...] = typing.get_args(AttemptState.__value__)
_JOB_ATTEMPT_STATES: tuple[str, ...] = typing.get_args(JobAttemptState.__value__)


def _count(query: Select[Any]) -> ColumnElement[int]:
    return query.with_only_columns(func.count(), maintain_column_froms=True).scalar_subquery()


def _tally(query: Select[Any], column: Any, states: tuple[str, ...]) -> list[ColumnElement[int]]:
    """One count per state of the rows ``query`` selects."""
    return [_count(query.where(column == state)) for state in states]


def _job_columns(job_id: JobID) -> list[ColumnElement[int]]:
    return _tally(
        select(job_attempts).where(job_attempts.c.job_id == job_id),
        job_attempts.c.state,
        _JOB_ATTEMPT_STATES,
    )


def _target_columns(
    job_id: JobID, child_ids: Select[Any], step_keys: Select[Any]
) -> list[ColumnElement[Any]]:
    """Monotone target facts plus a digest for mutable cache entries, at either query scope."""
    child_attempts = select(attempts).where(attempts.c.invocation_id.in_(child_ids))
    # Other jobs can replace or finish a step attempt at a key this job is waiting on.
    step_attempts = select(attempts).where(
        attempts.c.kind == "step", attempts.c.cache_key.in_(step_keys)
    )
    entry = (
        step_cache.c.cache_key
        + literal(":")
        + func.coalesce(
            func.cast(step_cache.c.attempt_id, Text),
            literal("result ") + func.cast(step_cache.c.step_result_id, Text),
        )
    )
    cache_digest = (
        select(
            func.md5(
                func.coalesce(func.string_agg(entry, aggregate_order_by(literal(","), entry)), "")
            )
        )
        .where(step_cache.c.cache_key.in_(step_keys))
        .scalar_subquery()
    )
    return [
        _count(
            select(workflow_invocations).where(
                workflow_invocations.c.id.in_(child_ids),
                workflow_invocations.c.result_cid.is_not(None),
            )
        ),
        _count(_bindings_at(job_id, step_keys)),
        *_tally(child_attempts, attempts.c.state, _ATTEMPT_STATES),
        *_tally(step_attempts, attempts.c.state, _ATTEMPT_STATES),
        cache_digest,
    ]


def _bindings_at(job_id: JobID, step_keys: Select[Any]) -> Select[Any]:
    """The job's bindings at ``step_keys``. The job ID is a subquery, not a constant, for the
    reason in :mod:`fxtr.runner.postgres.status_queries` ("Planning without the job IDs"): with a
    constant, a job missing from the planner's statistics makes this a semi join whose cost is the
    job's bindings times the keys."""
    job = select(jobs.c.id).where(jobs.c.id == job_id).scalar_subquery()
    return select(job_step_bindings).where(
        job_step_bindings.c.job_id == job, job_step_bindings.c.cache_key.in_(step_keys)
    )


async def read_invocation_version(
    tx: Transaction, job_id: JobID, invocation_id: InvocationID, *, finished: bool
) -> str:
    """Read a complete invocation's revision in the caller's snapshot."""
    own_attempts = select(attempts).where(attempts.c.invocation_id == invocation_id)
    own_attempt_ids = select(attempts.c.id).where(attempts.c.invocation_id == invocation_id)
    child_ids = select(spawns.c.child_invocation_id).where(
        spawns.c.invocation_id == invocation_id, spawns.c.child_invocation_id.is_not(None)
    )
    step_keys = select(spawns.c.step_cache_key).where(
        spawns.c.invocation_id == invocation_id, spawns.c.step_cache_key.is_not(None)
    )
    columns = [
        _count(select(nodes).where(nodes.c.invocation_id == invocation_id)),
        _count(select(spawns).where(spawns.c.invocation_id == invocation_id)),
        _count(select(attempt_events).where(attempt_events.c.attempt_id.in_(own_attempt_ids))),
        _count(select(attempt_logs).where(attempt_logs.c.attempt_id.in_(own_attempt_ids))),
        *_tally(own_attempts, attempts.c.state, _ATTEMPT_STATES),
        *_target_columns(job_id, child_ids, step_keys),
        *_job_columns(job_id),
    ]
    row = (await tx.execute(select(*columns))).one()
    return ".".join(str(n) for n in (int(finished), *row))


async def read_run_versions(
    tx: Transaction, job_id: JobID, invocation_id: InvocationID
) -> dict[bytes, str]:
    """All expanded runs' revisions in one query, independent of the number of mapped targets."""
    runs = (
        select(spawns.c.node_identity)
        .where(spawns.c.invocation_id == invocation_id)
        .distinct()
        .cte("version_runs")
    )
    selected = (
        select(spawns)
        .where(
            spawns.c.invocation_id == invocation_id, spawns.c.node_identity == runs.c.node_identity
        )
        .correlate(runs)
    )
    child_ids = selected.with_only_columns(spawns.c.child_invocation_id).where(
        spawns.c.child_invocation_id.is_not(None)
    )
    step_keys = selected.with_only_columns(spawns.c.step_cache_key).where(
        spawns.c.step_cache_key.is_not(None)
    )
    rows = (
        await tx.execute(
            select(
                runs.c.node_identity,
                _count(selected),
                *_target_columns(job_id, child_ids, step_keys),
                *_job_columns(job_id),
            )
        )
    ).all()
    return {row.node_identity: ".".join(str(n) for n in row[1:]) for row in rows}


async def invocation_version(database: Database, job_id: JobID, pathname: str) -> str | None:
    """The invocation's version, or ``None`` if the job has no invocation at that pathname."""
    async with database.transaction(snapshot=True) as tx:
        found = (
            await tx.execute(
                select(workflow_invocations.c.id, workflow_invocations.c.result_cid).where(
                    workflow_invocations.c.job_id == job_id,
                    workflow_invocations.c.pathname == pathname,
                )
            )
        ).first()
        if found is None:
            return None
        return await read_invocation_version(
            tx, job_id, found.id, finished=found.result_cid is not None
        )
