"""Which jobs a viewer reader may show: :class:`JobScope`.

A database holds many jobs, and they share one step cache and one entity store. A reader may be
allowed to show only some of its jobs: the hub serves a project's tables to readers whose
permissions cover some of the project's jobs, while ``fxtr view`` shows everything in the
project's own database. The scope is the set of jobs a reader may show, and these rules make it
the only thing a reader reveals:

* Everything a job-keyed query returns is filtered by the scope before pagination (``jobs``,
  ``jobSummaries``). A job-keyed query for one job outside the scope answers exactly as it does
  for a job the database does not have: ``null`` where the field is nullable, the same
  :class:`~fxtr.runner.postgres.ledger.NotFoundError` where it is not, so a job outside the
  scope cannot be told from one that does not exist.
* A job summary compares its job with other jobs of the same workflow (which inputs changed
  since the job before it, and whether a later job exists). Only jobs in the scope count as
  other jobs. A summary gives the text of an input only where the input's array is reachable,
  as for any entity (below).
* A step result in the cache may have been produced by a job outside the scope. The step's
  result, its state, and its provenance (which job attempt and code produced it, the fact that
  the result was reused) are shown; the producer's attempt history (the attempt row with its
  error, its events, and its ``ctx.log`` lines) is shown only when the producer's job is in the
  scope. The same holds for the attempt behind a step cache entry.
* Entities and typed loads (raw entity bytes, ``entity``, ``entityMetas``, ``array``, and an
  output source naming an array by cid) are served only when
  :data:`~fxtr.runner.postgres.schema.job_entities` names the cid for a job in the scope (one
  query per request for the requested cids, :func:`reachable`); a cid it does not name answers
  as not stored. ``job_entities`` is filled when a job record is imported, so under a restricted
  scope the entities of a job this database ran itself are unreachable; the unrestricted scope
  never consults it.

Every root field of ``schema.graphql`` is in :data:`ROOT_FIELDS`, which says how the scope
applies to it (a test checks that the two agree, so a new field cannot be forgotten):

===================== ============ =======================================================
Root field            Treatment    Why
===================== ============ =======================================================
``jobs``              filtered     the job list, filtered before its per-job status reads
``jobSummaries``      filtered     the paged job list, filtered in SQL before paging
``jobSummary``        checked      one job: ``null`` outside the scope
``jobSummariesByIds`` filtered     the jobs named that are in the scope, the others left out
                                   as jobs the database does not have
``jobWorkflows``      filtered     the workflow names of the jobs in the scope
``jobReuse``          checked      one job: no nodes outside the scope, as for a missing job;
                                   the counts are the job's own facts
``job``               checked      one job: ``NotFoundError`` outside the scope, as for a
                                   missing job
``levelOverview``     checked      one invocation: ``null`` outside the scope; its contents
                                   are the job's own facts (``oldestRunningSince`` is the
                                   start time of whichever attempt runs a shared key, as its
                                   schema says)
``runPage``           checked      one run: ``NotFoundError`` outside the scope, as for a
                                   missing invocation
``keyPage``           checked      as ``runPage``
``invocationPage``    checked      as ``runPage``
``invocation``        checked      ``null`` outside the scope; its attempts are the job's own
``invocationVersion`` checked      ``null`` outside the scope
``step``              checked      ``null`` outside the scope; the producer's attempt and
                                   logs only when the producer's job is in the scope
``stepCache``         filtered     entries whose offered result a job in the scope bound, or
                                   whose attempt ran under a job in the scope (the cache is
                                   global and mutable, so a key a job requested may since hold
                                   another job's request); attempt and logs only in the
                                   second case
``outputPage``        checked      a run source as ``runPage``; an array source by
                                   reachability
``outputDimValues``   checked      as ``outputPage``
``array``             reachability served only for a reachable cid
``entity``            reachability served only for a reachable cid
``entityMetas``       reachability only the reachable cids among those asked for
===================== ============ =======================================================

The raw entity routes ``GET /api/entity/{cid}`` and ``POST /api/entities`` follow the
reachability rule too. The scope is a value: :meth:`JobScope.all` is unrestricted (what
``fxtr view`` uses), :meth:`JobScope.of` names the allowed jobs, and the reader holding it
(:class:`~fxtr.runner.postgres.debug_web.reader.ViewerReader`) is built per request by a caller
that computed the set, the hub.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, select, true

from fxtr.runner.postgres.database import Transaction
from fxtr.runner.postgres.ledger import JobID, NotFoundError
from fxtr.runner.postgres.schema import job_entities

type Treatment = Literal["filtered", "checked", "reachability"]

ROOT_FIELDS: Mapping[str, Treatment] = {
    "jobs": "filtered",
    "jobSummaries": "filtered",
    "jobSummary": "checked",
    "jobSummariesByIds": "filtered",
    "jobWorkflows": "filtered",
    "jobReuse": "checked",
    "job": "checked",
    "levelOverview": "checked",
    "runPage": "checked",
    "keyPage": "checked",
    "invocationPage": "checked",
    "invocation": "checked",
    "invocationVersion": "checked",
    "step": "checked",
    "stepCache": "filtered",
    "outputPage": "checked",
    "outputDimValues": "checked",
    "array": "reachability",
    "entity": "reachability",
    "entityMetas": "reachability",
}
"""How the scope applies to each root field of ``schema.graphql`` (the table in the module
docstring says why). Every root field is here; ``test_debug_web_scope.py`` checks it against
the schema."""


@dataclass(frozen=True)
class JobScope:
    """The jobs a reader may show (see the module docstring for the rules).

    Attributes:
        job_ids: The allowed jobs, or ``None`` for every job.
    """

    job_ids: frozenset[JobID] | None

    @classmethod
    def all(cls) -> JobScope:
        """The unrestricted scope: every job in the database, as ``fxtr view`` shows."""
        return cls(None)

    @classmethod
    def of(cls, job_ids: Iterable[UUID]) -> JobScope:
        """The scope allowing exactly ``job_ids`` (possibly none)."""
        return cls(frozenset(JobID(job_id) for job_id in job_ids))

    @property
    def unrestricted(self) -> bool:
        """Whether every job is allowed."""
        return self.job_ids is None

    def allows(self, job_id: UUID) -> bool:
        """Whether the reader may show ``job_id``."""
        return self.job_ids is None or job_id in self.job_ids

    def restrict(self, column: ColumnElement[Any]) -> ColumnElement[bool]:
        """A ``WHERE`` clause keeping the rows whose job id ``column`` is allowed: true for the
        unrestricted scope."""
        if self.job_ids is None:
            return true()
        return column.in_(list(self.job_ids))


def require_job(scope: JobScope, job_id: JobID) -> None:
    """Refuse a job outside the scope the way a missing job is refused.

    Raises:
        NotFoundError: If the scope does not allow ``job_id``; the same error, with the same
            message, as :func:`~fxtr.runner.postgres.ledger.get_job` raises for a job the
            database does not have.
    """
    if not scope.allows(job_id):
        raise NotFoundError(f"No job {job_id}")


def require_invocation(scope: JobScope, job_id: JobID, pathname: str) -> None:
    """Refuse an invocation of a job outside the scope the way a missing invocation is refused.

    Raises:
        NotFoundError: If the scope does not allow ``job_id``; the same error, with the same
            message, as :func:`~fxtr.runner.postgres.ledger.find_invocation` raises for an
            invocation the database does not have.
    """
    if not scope.allows(job_id):
        raise NotFoundError(f"Job {job_id} has no invocation at {pathname!r}")


async def reachable(tx: Transaction, scope: JobScope, cids: Collection[bytes]) -> set[bytes]:
    """The cids among ``cids`` the scope may serve: all of them for the unrestricted scope,
    without a query; otherwise the ones ``job_entities`` names for a job in the scope, read in
    one query."""
    if scope.unrestricted:
        return set(cids)
    if not cids:
        return set()
    rows = await tx.execute(
        select(job_entities.c.cid)
        .where(job_entities.c.cid.in_(list(set(cids))), scope.restrict(job_entities.c.job_id))
        .distinct()
    )
    return {bytes(row.cid) for row in rows}
