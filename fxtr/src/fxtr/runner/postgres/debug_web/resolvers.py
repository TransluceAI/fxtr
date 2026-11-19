"""GraphQL resolvers for the debug viewer, reading through a :class:`ViewerReader`.

Every resolver builds plain dicts (and lists of them) keyed by the schema's field names, so
Ariadne's default resolver reads them with no per-type binding; only the scalars and the ``Query``
root are bound. Nothing here writes: it reads jobs and entities through ``ViewerReader`` and
queries the global step cache through the ledger, execution, and log readers in a read transaction.
Every root field applies the reader's :class:`~fxtr.runner.postgres.debug_web.scope.JobScope`
as the table in :mod:`.scope` says.

The job view is assembled eagerly from :meth:`Jobs.describe` and :meth:`Jobs.status` — one
consistent picture per job; the jobs *list* is shallow (a
``status`` call per row) since the front page shows only each job's state.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Any

from ariadne import QueryType, ScalarType
from graphql import GraphQLResolveInfo
from sqlalchemy import select

from fxtr.core import codec
from fxtr.core.entity_data import EntityID, EntityNotFoundError, cid_from_str
from fxtr.entity_defns.array import Array
from fxtr.runner.postgres import execution, ledger
from fxtr.runner.postgres.debug_web import browse
from fxtr.runner.postgres.debug_web.contracts import (
    ColumnFilter,
    EntityMeta,
    OutputSource,
    Sort,
)
from fxtr.runner.postgres.debug_web.projections import error_view, node_view
from fxtr.runner.postgres.debug_web.reader import ViewerReader
from fxtr.runner.postgres.debug_web.scope import JobScope, reachable, require_job
from fxtr.runner.postgres.debug_web.versions import invocation_version
from fxtr.runner.postgres.execution import JobAttemptRecord
from fxtr.runner.postgres.jobs import (
    InvocationDetail,
    InvocationStatus,
    Provenance,
    StepDetail,
    StepStatus,
    provenance,
)
from fxtr.runner.postgres.json_values import array_type_to_json
from fxtr.runner.postgres.ledger import (
    EventRecord,
    JobID,
    SpawnRecord,
)
from fxtr.runner.postgres.logs import LogRecord, list_logs
from fxtr.runner.postgres.schema import job_step_bindings


def _reader(info: GraphQLResolveInfo) -> ViewerReader:
    return info.context["reader"]


def _job_id(value: str) -> JobID:
    import uuid

    return JobID(uuid.UUID(value))


def _cid(bound: Any) -> str | None:
    """The DASL CID string of a ``BoundID`` (or ``None``)."""
    return None if bound is None else bound.entity_id.cid_str()


# ---- record → dict builders (keys match the schema's field names) ----


def _event(event: EventRecord) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "kind": event.kind,
        "identity": event.identity.cid_str(),
        "at": event.at,
    }


def _log(log: LogRecord) -> dict[str, Any]:
    return {"seq": log.seq, "message": log.message, "at": log.at}


def _provenance(provenance: Provenance | None) -> dict[str, Any] | None:
    if provenance is None:
        return None
    job_attempt, code_version = provenance.job_attempt, provenance.code_version
    return {
        "jobId": job_attempt.job_id,
        "jobAttemptId": job_attempt.id,
        "commit": code_version.commit,
        "tree": code_version.tree,
        "ephemeral": code_version.ephemeral,
        "projectPath": code_version.project_path,
        "environmentVerified": code_version.environment_verified,
    }


def _attempt(
    attempt: Any, events: list[EventRecord], logs: list[LogRecord], provenance: Provenance
) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "kind": attempt.kind,
        "state": attempt.state,
        "error": error_view(attempt.error),
        "reason": attempt.reason,
        "stoppedOn": None
        if attempt.stopped_on is None
        else [n.cid_str() for n in attempt.stopped_on],
        "resultCid": _cid(attempt.result),
        "startedAt": attempt.started_at,
        "ownerJobAttemptId": attempt.owner_job_attempt_id,
        "provenance": _provenance(provenance),
        "finishedAt": attempt.finished_at,
        "events": [_event(e) for e in events],
        "logs": [_log(log) for log in logs],
    }


def _spawn(spawn: SpawnRecord, detail: InvocationDetail) -> dict[str, Any]:
    child = spawn.child_invocation_id
    if child is not None:
        result = detail.child_results[child]
        state: str = detail.child_states[child]
    else:
        assert spawn.step_cache_key is not None, "a spawn names a child or a step"
        result = detail.step_results[spawn.step_cache_key]
        state = detail.step_states[spawn.step_cache_key]
    return {
        "nodeIdentity": spawn.node_identity.cid_str(),
        "key": list(spawn.key),
        "childPathname": None if child is None else detail.child_pathnames[child],
        "stepCacheKey": spawn.step_cache_key,
        "resultCid": _cid(result),
        "state": state,
    }


def _job_attempt(job_attempt: JobAttemptRecord, stale_after: timedelta) -> dict[str, Any]:
    return {
        "id": job_attempt.id,
        "seq": job_attempt.seq,
        "state": job_attempt.state,
        "reason": job_attempt.reason,
        "owner": job_attempt.owner,
        "heartbeatAt": job_attempt.heartbeat_at,
        # Display only: a stale lease changes nothing by itself, but a resume may take the job
        # over, and it judges by the same threshold, the project's.
        "leaseFresh": job_attempt.fresh(stale_after),
        "evictedAt": job_attempt.evicted_at,
        "evictedReason": job_attempt.evicted_reason,
        "createdAt": job_attempt.created_at,
        "settledAt": job_attempt.settled_at,
    }


def _invocation_summary(status: InvocationStatus) -> dict[str, Any]:
    """A row in a job's invocation list (see the ``InvocationSummary`` schema type)."""
    return {
        "pathname": status.pathname,
        "workflowName": status.workflow_name,
        "state": status.state,
    }


def _step_summary(status: StepStatus) -> dict[str, Any]:
    """A row in a job's step list (see the ``StepSummary`` schema type)."""
    return {"cacheKey": status.cache_key, "stepName": status.step_name, "state": status.state}


def _invocation(detail: InvocationDetail, job_id: JobID) -> dict[str, Any]:
    description = detail.description
    invocation = description.invocation
    status = detail.status
    pathname = invocation.pathname
    return {
        "id": f"{job_id}/{pathname}",
        "pathname": pathname,
        "workflowName": invocation.workflow_name,
        "cacheKeyPrefix": invocation.cache_key_prefix,
        "state": status.state,
        "error": error_view(status.error),
        "reason": status.reason,
        "resultCid": _cid(invocation.result),
        "provenance": _provenance(description.result_provenance),
        "nodes": [node_view(n, job_id, pathname) for n in description.nodes],
        "spawns": [_spawn(s, detail) for s in description.spawns],
        "attempts": [
            _attempt(a.attempt, a.events, a.logs, a.provenance) for a in description.attempts
        ],
    }


def _step_pin(detail: StepDetail, job_id: JobID, scope: JobScope) -> dict[str, Any]:
    description = detail.description
    # The attempt behind the step ran under the job attempt its provenance names: another job's,
    # for a result reused from the cache. Its history shows only when that job is in the scope;
    # the result and its provenance are the step's own facts.
    attempt = _shown_attempt(scope, description.attempt, description.logs, description.provenance)
    return {
        "id": f"{job_id}/{description.request.cache_key}",
        "cacheKey": description.request.cache_key,
        "stepName": description.spec.step_name,
        "requestCid": description.request.request.cid_str(),
        "state": detail.status.state,
        "resultCid": _cid(description.result),
        "resultTaken": description.result_taken,
        "provenance": _provenance(description.result_provenance),
        "attempt": attempt,
        "logs": [] if attempt is None else [_log(log) for log in description.logs],
        # A preview is part of the producing attempt's history: shown when that attempt is.
        "preview": None
        if attempt is None or description.attempt is None or description.preview is None
        else {
            "attemptId": description.attempt.id,
            "seq": description.preview.seq,
            "at": description.preview.at,
        },
    }


def _shown_attempt(
    scope: JobScope, attempt: Any, logs: list[LogRecord], provenance: Provenance | None
) -> dict[str, Any] | None:
    """``attempt`` (which ran under the job attempt ``provenance`` names) with its logs as the
    schema shows it, or ``None`` when there is none or its job is outside the scope."""
    if attempt is None or provenance is None or not scope.allows(provenance.job_attempt.job_id):
        return None
    return _attempt(attempt, [], logs, provenance)


# ---- assembling a whole job ----


async def _job_overview(reader: ViewerReader, job_id: JobID) -> dict[str, Any]:
    """A job's overview + shallow invocation/step lists (no nodes). The full invocation / step is
    fetched separately via :func:`_invocation_view` / :func:`_step_view`."""
    require_job(reader.scope, job_id)
    status = await reader.jobs.status(job_id)
    return {
        "id": job_id,
        "workflowName": status.job.workflow_name,
        "root": status.job.root,
        "createdAt": status.job.created_at,
        "state": status.state,
        "failed": status.failed,
        "jobAttempts": [_job_attempt(ja, reader.stale_after) for ja in status.job_attempts],
        "invocations": [_invocation_summary(s) for s in status.invocations.values()],
        "steps": [_step_summary(s) for s in status.steps.values()],
        "resultCid": _cid(status.result),
    }


async def _invocation_view(
    reader: ViewerReader, job_id: JobID, pathname: str
) -> dict[str, Any] | None:
    if not reader.scope.allows(job_id):
        return None
    detail = await reader.jobs.describe_invocation(job_id, pathname)
    return None if detail is None else _invocation(detail, job_id)


async def _step_view(reader: ViewerReader, job_id: JobID, cache_key: str) -> dict[str, Any] | None:
    if not reader.scope.allows(job_id):
        return None
    detail = await reader.jobs.describe_step(job_id, cache_key)
    return None if detail is None else _step_pin(detail, job_id, reader.scope)


async def _jobs_shallow(reader: ViewerReader) -> list[dict[str, Any]]:
    jobs = []
    for record in await reader.jobs.list():
        if not reader.scope.allows(record.id):
            continue
        status = await reader.jobs.status(record.id)
        jobs.append(
            {
                "id": record.id,
                "workflowName": record.workflow_name,
                "root": record.root,
                "createdAt": record.created_at,
                "state": status.state,
                "failed": status.failed,
            }
        )
    return jobs


async def _cache_entries(reader: ViewerReader) -> list[dict[str, Any]]:
    """The step cache's entries. The cache is global and mutable: a key a job in the scope
    requested may since have been cleared and taken by another job's request. So under a
    restricted scope an entry shows only when what it holds belongs to the scope: the step
    result it offers is bound by a job in the scope, or the attempt behind it ran under a job in
    the scope; its attempt and logs show only in the second case."""
    scope = reader.scope
    entries: list[dict[str, Any]] = []
    async with reader.database.transaction(snapshot=True) as tx:
        bound: set[Any] = set()
        if not scope.unrestricted:
            bound = set(
                (
                    await tx.execute(
                        select(job_step_bindings.c.step_result_id).where(
                            scope.restrict(job_step_bindings.c.job_id)
                        )
                    )
                ).scalars()
            )
        for entry in await execution.list_cache_entries(tx):
            if entry.step_result_id is not None:
                # The step result it offers, and its producing attempt if this database ran it.
                attempt = await execution.find_attempt(
                    tx, ledger.producing_attempt(entry.step_result_id)
                )
                step_result = await ledger.get_step_result(tx, entry.step_result_id)
                produced = await provenance(tx, reader.entities, step_result.job_attempt_id)
                state = "completed"
            else:
                assert entry.attempt_id is not None
                attempt = await execution.get_attempt(tx, entry.attempt_id)
                produced = await provenance(tx, reader.entities, attempt.owner_job_attempt_id)
                state = attempt.state
            own = scope.allows(produced.job_attempt.job_id)
            if not own and entry.step_result_id not in bound:
                continue
            logs = await list_logs(tx, attempt.id) if own and attempt is not None else []
            spec = await ledger.get_step_request(tx, entry.request)
            entries.append(
                {
                    "id": entry.cache_key,
                    "cacheKey": entry.cache_key,
                    "requestCid": entry.request.cid_str(),
                    "stepName": spec.step_name,
                    "attempt": _shown_attempt(scope, attempt, logs, produced),
                    "state": state,
                    "provenance": _provenance(produced),
                }
            )
    return entries


async def _array_view(reader: ViewerReader, cid: str, limit: int | None = None) -> dict[str, Any]:
    entity_id = EntityID(cid=cid_from_str(cid))
    if not await reader.reachable([entity_id]):
        # The same answer as for an array the database does not have.
        raise EntityNotFoundError(f"Entity not found in storage: {entity_id!r}")
    array = await reader.entities.load(entity_id.bind_type(Array[Any]))
    items = list(array.items())
    shown = items if limit is None else items[:limit]
    return {
        "cid": cid,
        "type": array_type_to_json(array.type),
        "rowCount": len(items),
        "rows": [
            {"key": list(key), "value": base64.b64encode(codec.encode(value)).decode("ascii")}
            for key, value in shown
        ],
    }


async def _entity_meta(reader: ViewerReader, cid: str) -> EntityMeta | None:
    if not await reader.reachable([EntityID(cid=cid_from_str(cid))]):
        return None
    return await browse.entity_meta(reader.database, cid)


# ---- bindings ----

query = QueryType()


@query.field("outputPage")
async def resolve_output_page(
    _: Any,
    info: GraphQLResolveInfo,
    source: OutputSource,
    offset: int = 0,
    first: int = 25,
    filters: list[ColumnFilter] | None = None,
    sort: Sort | None = None,
    last: bool = False,
):
    request = info.context.get("request")
    return await info.context["outputs"].page(
        source,
        offset,
        first,
        {item["key"]: item["text"] for item in filters or []},
        sort,
        disconnected=getattr(request, "is_disconnected", None),
        last=last,
    )


@query.field("invocationPage")
async def resolve_invocation_page(
    _: Any,
    info: GraphQLResolveInfo,
    jobId: str,
    pathname: str,
    nodeId: str,
    selection: dict,
    offset: int = 0,
    first: int = 25,
    filters: list[ColumnFilter] | None = None,
    sort: Sort | None = None,
    state: str | None = None,
    last: bool = False,
):
    request = info.context.get("request")
    return await info.context["outputs"].invocation_page(
        _job_id(jobId),
        pathname,
        nodeId,
        selection,
        offset,
        first,
        {item["key"]: item["text"] for item in filters or []},
        sort,
        state,
        last,
        disconnected=getattr(request, "is_disconnected", None),
    )


@query.field("outputDimValues")
async def resolve_output_dim_values(
    _: Any,
    info: GraphQLResolveInfo,
    source: OutputSource,
    dim: str,
    search: str = "",
    offset: int = 0,
    first: int = 25,
):
    request = info.context.get("request")
    return await info.context["outputs"].dim_values(
        source,
        dim,
        search,
        offset,
        first,
        disconnected=getattr(request, "is_disconnected", None),
    )


@query.field("entityMetas")
async def resolve_entity_metas(_: Any, info: GraphQLResolveInfo, cids: list[str]):
    from fxtr.core.entity_data import cid_to_str
    from fxtr.runner.postgres.schema import entities

    if len(cids) > 200:
        raise ValueError("at most 200 entity metadata entries per request")
    reader = _reader(info)
    async with reader.database.transaction() as tx:
        asked = await reachable(tx, reader.scope, [cid_from_str(cid) for cid in cids])
        rows = (
            await tx.execute(
                select(entities.c.cid, entities.c.type_tag).where(entities.c.cid.in_(list(asked)))
            )
        ).all()
    return [{"cid": cid_to_str(row.cid), "typeTag": row.type_tag} for row in rows]


@query.field("jobSummaries")
async def resolve_job_summaries(
    _: Any,
    info: GraphQLResolveInfo,
    first: int = 25,
    after: str | None = None,
    search: str = "",
    workflow: str | None = None,
):
    return await browse.job_page(_reader(info), first, after, search, workflow)


@query.field("jobWorkflows")
async def resolve_job_workflows(_: Any, info: GraphQLResolveInfo):
    return await browse.job_workflows(_reader(info))


@query.field("jobSummary")
async def resolve_job_summary(_: Any, info: GraphQLResolveInfo, id: str):
    found = await browse.job_summaries_by_ids(_reader(info), [_job_id(id)])
    return found[0] if found else None


@query.field("jobSummariesByIds")
async def resolve_job_summaries_by_ids(_: Any, info: GraphQLResolveInfo, ids: list[str]):
    return await browse.job_summaries_by_ids(_reader(info), [_job_id(id) for id in ids])


@query.field("jobReuse")
async def resolve_job_reuse(_: Any, info: GraphQLResolveInfo, jobId: str):
    return await browse.job_reuse(_reader(info), _job_id(jobId))


@query.field("levelOverview")
async def resolve_level_overview(_: Any, info: GraphQLResolveInfo, jobId: str, pathname: str):
    reader = _reader(info)
    return await browse.level_overview(reader.database, _job_id(jobId), pathname, reader.scope)


@query.field("runPage")
async def resolve_run_page(
    _: Any,
    info: GraphQLResolveInfo,
    jobId: str,
    pathname: str,
    nodeId: str,
    selection: dict,
    first: int = 25,
    after: str | None = None,
    state: str | None = None,
    unfinished: bool = False,
    offset: int | None = None,
):
    reader = _reader(info)
    return await browse.run_page(
        reader.database,
        _job_id(jobId),
        pathname,
        nodeId,
        selection,
        first,
        after,
        state,
        unfinished,
        offset,
        reader.scope,
    )


@query.field("keyPage")
async def resolve_key_page(
    _: Any,
    info: GraphQLResolveInfo,
    jobId: str,
    pathname: str,
    nodeId: str,
    dim: str,
    selection: dict,
    search: str = "",
    first: int = 25,
    after: str | None = None,
    offset: int | None = None,
):
    reader = _reader(info)
    return await browse.key_page(
        reader.database,
        _job_id(jobId),
        pathname,
        nodeId,
        dim,
        selection,
        search,
        first,
        after,
        offset,
        reader.scope,
    )


@query.field("jobs")
async def resolve_jobs(_: Any, info: GraphQLResolveInfo) -> list[dict[str, Any]]:
    return await _jobs_shallow(_reader(info))


@query.field("job")
async def resolve_job(_: Any, info: GraphQLResolveInfo, id: str) -> dict[str, Any]:
    return await _job_overview(_reader(info), _job_id(id))


@query.field("invocation")
async def resolve_invocation(
    _: Any, info: GraphQLResolveInfo, jobId: str, pathname: str
) -> dict[str, Any] | None:
    return await _invocation_view(_reader(info), _job_id(jobId), pathname)


@query.field("invocationVersion")
async def resolve_invocation_version(
    _: Any, info: GraphQLResolveInfo, jobId: str, pathname: str
) -> str | None:
    reader = _reader(info)
    job_id = _job_id(jobId)
    if not reader.scope.allows(job_id):
        return None
    return await invocation_version(reader.database, job_id, pathname)


@query.field("step")
async def resolve_step(
    _: Any, info: GraphQLResolveInfo, jobId: str, cacheKey: str
) -> dict[str, Any] | None:
    return await _step_view(_reader(info), _job_id(jobId), cacheKey)


@query.field("stepCache")
async def resolve_step_cache(_: Any, info: GraphQLResolveInfo) -> list[dict[str, Any]]:
    return await _cache_entries(_reader(info))


@query.field("array")
async def resolve_array(
    _: Any, info: GraphQLResolveInfo, cid: str, limit: int | None = None
) -> dict[str, Any]:
    return await _array_view(_reader(info), cid, limit)


@query.field("entity")
async def resolve_entity(_: Any, info: GraphQLResolveInfo, cid: str) -> EntityMeta | None:
    return await _entity_meta(_reader(info), cid)


# ---- scalars ----

datetime_scalar = ScalarType("DateTime")


@datetime_scalar.serializer
def _serialize_datetime(value: datetime) -> str:
    return value.isoformat()


json_scalar = ScalarType("JSON")


@json_scalar.serializer
def _serialize_json(value: Any) -> Any:
    return value


BINDABLES = [query, datetime_scalar, json_scalar]
"""Everything :func:`make_executable_schema` binds. ``Cid`` and ``Base64`` need no binding: they
carry plain strings and pass through unchanged."""
