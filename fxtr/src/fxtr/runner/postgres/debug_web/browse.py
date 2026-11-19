"""Payload-free viewer reads and bounded navigation over the runner's relational records.

Status comes from the runner's canonical status relations. These readers load no entity bytes
or logs for summaries and navigation. Each takes the reader's :class:`JobScope` (every job by
default): the job page is filtered by it, and a query for one job outside it answers as for a
missing job (:mod:`.scope`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Text, and_, case, cast, exists, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB

from fxtr.core.entity_data import EntityID, cid_from_str, cid_to_str
from fxtr.entity_defns.array import Array
from fxtr.project.code_version import CodeVersion
from fxtr.runner.postgres import execution, ledger
from fxtr.runner.postgres.database import Database, Transaction
from fxtr.runner.postgres.ledger import JobID
from fxtr.runner.postgres.schema import (
    attempt_events,
    attempts,
    entities,
    job_attempt_provenance,
    job_attempts,
    job_step_bindings,
    job_step_requests,
    jobs,
    nodes,
    spawns,
    step_requests,
    step_results,
    workflow_invocations,
)
from fxtr.runner.postgres.status_queries import StatusQueries
from fxtr.runner.session import AttemptID

from .contracts import (
    EntityMeta,
    FailureGroup,
    JobInput,
    JobPage,
    JobSummary,
    KeyPage,
    LevelOverview,
    NodeReuse,
    RunActivity,
    RunPage,
    SpawnView,
    StateCounts,
    state_counts,
)
from .git_history import Launch
from .projections import error_view, no_activity, overview_node
from .reader import ViewerReader
from .scope import JobScope, require_invocation
from .versions import read_invocation_version, read_run_versions


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


# A dim's values are read whole up to this many, for the viewer to scroll through without paging.
KEY_LIST_LIMIT = 1000


def page_size(first: int, maximum: int = 200) -> int:
    if not 1 <= first <= maximum:
        raise ValueError(f"page size must be between 1 and {maximum}")
    return first


def page_offset(offset: int, first: int, total: int, last: bool = False) -> int:
    """Where the page holding row `offset` starts: snapped to a page boundary, and clamped to the
    last page (which `last` asks for whatever `offset` is)."""
    final = max(0, (total - 1) // first * first)
    return final if last else min(offset // first * first, final)


def cursor(scope: Any, position: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps([digest(scope), position]).encode()).decode()


def position(after: str | None, scope: Any) -> Any:
    if after is None:
        return None
    try:
        scope_hash, value = json.loads(base64.urlsafe_b64decode(after))
        if scope_hash == digest(scope):
            return value
    except (ValueError, TypeError):
        pass
    raise ValueError("invalid cursor for this query; refresh the page")


def spawn_dict(row: Any) -> SpawnView:
    return {
        "nodeIdentity": cid_to_str(row.node_identity),
        "key": row.key,
        "childPathname": row.child_pathname,
        "stepCacheKey": row.step_cache_key,
        "resultCid": None if row.result_cid is None else cid_to_str(row.result_cid),
        "state": row.state,
    }


async def job_summaries(
    tx: Transaction, job_ids: Sequence[JobID], scope: JobScope = JobScope.all()
) -> dict[JobID, JobSummary]:
    """One grouped status query for the requested jobs, regardless of page or history size.

    The caller passes only jobs the scope allows. ``scope`` limits the other jobs a summary is
    compared with (:func:`_compare_with_other_jobs`).
    """
    states = StatusQueries(job_ids)
    inv, step, context = states.invocations.c, states.steps.c, states.jobs.c
    inv_totals = (
        select(
            inv.job_id,
            func.bool_or(inv.display_state.in_(["queued", "running"])).label("live"),
            func.bool_or(inv.display_state == "failed").label("failed"),
        )
        .group_by(inv.job_id)
        .subquery()
    )
    step_totals = (
        select(step.job_id, func.bool_or(step.display_state == "failed").label("failed"))
        .group_by(step.job_id)
        .subquery()
    )
    root = workflow_invocations.alias("root")
    state = case(
        (root.c.result_cid.is_not(None), "succeeded"),
        (inv_totals.c.live, "running"),
        else_="stopped",
    )
    rows = (
        await tx.execute(
            select(
                context.id,
                context.workflow_name,
                context.root,
                context.created_at,
                state.label("state"),
                and_(
                    state == "stopped",
                    or_(
                        func.coalesce(inv_totals.c.failed, False),
                        func.coalesce(step_totals.c.failed, False),
                    ),
                ).label("failed"),
            ).select_from(
                states.jobs.outerjoin(
                    root, and_(root.c.job_id == context.id, root.c.pathname == context.root)
                )
                .outerjoin(inv_totals, inv_totals.c.job_id == context.id)
                .outerjoin(step_totals, step_totals.c.job_id == context.id)
            )
        )
    ).all()
    failed_steps = await _failed_steps(tx, [row.id for row in rows if row.failed])
    inputs = await _root_inputs(tx, [row.id for row in rows])
    changed, latest = await _compare_with_other_jobs(tx, [row.id for row in rows], inputs, scope)
    return {
        row.id: {
            "id": str(row.id),
            "workflowName": row.workflow_name,
            "root": row.root,
            "createdAt": row.created_at,
            "state": row.state,
            "failed": row.failed,
            "failedSteps": failed_steps.get(row.id, []),
            "inputs": inputs.get(row.id, []),
            "changedInputs": changed.get(row.id, []),
            "latest": row.id in latest,
        }
        for row in rows
    }


MAX_FAILED_STEPS = 3
"""The most failed steps a job summary names."""


async def _failed_steps(tx: Transaction, job_ids: Sequence[JobID]) -> dict[JobID, list[str]]:
    """For each failed job, the names of the steps with a failed invocation, the most failures
    first, at most :data:`MAX_FAILED_STEPS`; for a job none of whose steps failed (a workflow's
    own body raised), the names of its failed workflows instead."""
    if not job_ids:
        return {}
    states = StatusQueries(job_ids)
    step, inv = states.steps.c, states.invocations.c
    step_rows = (
        await tx.execute(
            select(step.job_id, step_requests.c.step_name.label("name"), func.count().label("n"))
            .select_from(
                states.steps.join(
                    job_step_requests,
                    and_(
                        job_step_requests.c.job_id == step.job_id,
                        job_step_requests.c.cache_key == step.cache_key,
                    ),
                ).join(
                    step_requests, step_requests.c.request_cid == job_step_requests.c.request_cid
                )
            )
            .where(step.display_state == "failed")
            .group_by(step.job_id, step_requests.c.step_name)
        )
    ).all()
    names: dict[JobID, list[tuple[int, str]]] = {}
    for row in step_rows:
        names.setdefault(row.job_id, []).append((-row.n, row.name))
    without = [job_id for job_id in job_ids if job_id not in names]
    if without:
        inv_rows = (
            await tx.execute(
                select(inv.job_id, inv.workflow_name.label("name"), func.count().label("n"))
                .where(inv.display_state == "failed", inv.job_id.in_(without))
                .group_by(inv.job_id, inv.workflow_name)
            )
        ).all()
        for row in inv_rows:
            names.setdefault(row.job_id, []).append((-row.n, row.name))
    return {
        job_id: [name for _, name in sorted(found)[:MAX_FAILED_STEPS]]
        for job_id, found in names.items()
    }


async def _root_inputs(tx: Transaction, job_ids: Sequence[JobID]) -> dict[JobID, list[JobInput]]:
    """Each job's root workflow arguments by name: the array given (its cid) and whether it is a
    single value (an array with no dims), which :func:`input_texts` can then show."""
    if not job_ids:
        return {}
    rows = (
        await tx.execute(
            select(
                workflow_invocations.c.job_id,
                nodes.c.params["name"].astext.label("name"),
                nodes.c.value_cid,
                func.jsonb_array_length(nodes.c.type["dims"]).label("dims"),
            )
            .select_from(
                jobs.join(
                    workflow_invocations,
                    and_(
                        workflow_invocations.c.job_id == jobs.c.id,
                        workflow_invocations.c.pathname == jobs.c.root,
                    ),
                ).join(nodes, nodes.c.invocation_id == workflow_invocations.c.id)
            )
            .where(jobs.c.id.in_(job_ids), nodes.c.kind == "input")
            .order_by(nodes.c.params["name"].astext)
        )
    ).all()
    found: dict[JobID, list[JobInput]] = {}
    for row in rows:
        found.setdefault(row.job_id, []).append(
            {
                "name": row.name,
                "cid": cid_to_str(row.value_cid),
                "single": row.dims == 0,
                "text": None,
            }
        )
    return found


async def _compare_with_other_jobs(
    tx: Transaction,
    job_ids: Sequence[JobID],
    inputs: dict[JobID, list[JobInput]],
    scope: JobScope,
) -> tuple[dict[JobID, list[str]], set[JobID]]:
    """The two parts of a job summary that read other jobs than the one summarized, which every
    other part leaves alone: for each job, the names of its root inputs given a different array
    than in the job of the same workflow created just before it, or not given to that job at all
    (none for a job with no earlier job of its workflow); and the jobs no job of the same
    workflow was created after. Only jobs in ``scope`` count as other jobs, so a summary says
    nothing of a job the reader may not show."""
    if not job_ids:
        return {}, set()
    later, earlier, newer = jobs.alias("later"), jobs.alias("earlier"), jobs.alias("newer")
    is_latest = ~exists().where(
        newer.c.workflow_name == later.c.workflow_name,
        newer.c.created_at > later.c.created_at,
        scope.restrict(newer.c.id),
    )
    preceding = (
        select(earlier.c.id)
        .where(
            earlier.c.workflow_name == later.c.workflow_name,
            earlier.c.created_at < later.c.created_at,
            scope.restrict(earlier.c.id),
        )
        .order_by(earlier.c.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    rows = (
        await tx.execute(
            select(later.c.id, preceding.label("preceding"), is_latest.label("latest")).where(
                later.c.id.in_(list(job_ids))
            )
        )
    ).all()
    before = {row.id: row.preceding for row in rows if row.preceding is not None}
    their = await _root_inputs(tx, sorted(set(before.values())))
    changed: dict[JobID, list[str]] = {}
    for job_id, preceding_id in before.items():
        given = {item["name"]: item["cid"] for item in their.get(preceding_id, [])}
        changed[job_id] = [
            item["name"]
            for item in inputs.get(job_id, [])
            if given.get(item["name"]) != item["cid"]
        ]
    return changed, {row.id for row in rows if row.latest}


MAX_INPUT_TEXT = 80
"""The longest text a job summary shows for an input's value."""

_input_texts: dict[str, str | None] = {}
"""What :func:`input_texts` worked out, by array cid: content-addressed, so never stale."""


async def input_texts(reader: ViewerReader, summaries: Iterable[JobSummary]) -> None:
    """Fill in ``text`` on the summaries' inputs that are a single string, number, boolean, or
    null: the value, cut to :data:`MAX_INPUT_TEXT`. Any other input keeps ``None`` (a dataset, a
    struct), and is told apart from another job's by its cid alone. Each array is read once.

    An input's array is an entity, so its text is given only where the reader's scope reaches
    the array (:meth:`ViewerReader.reachable`); elsewhere it keeps ``None``.
    """
    summaries = list(summaries)
    single = {
        item["cid"]: EntityID(cid=cid_from_str(item["cid"]))
        for summary in summaries
        for item in summary["inputs"]
        if item["single"]
    }
    reached = await reader.reachable(list(single.values())) if single else set()
    for summary in summaries:
        for item in summary["inputs"]:
            cid = item["cid"]
            if not item["single"] or single[cid].cid not in reached:
                continue
            if cid not in _input_texts:
                if len(_input_texts) > 10_000:
                    _input_texts.clear()
                array = await reader.entities.load(single[cid].bind_type(Array[Any]))
                values = [value for _, value in array.items()]
                value = values[0] if len(values) == 1 else ...
                if isinstance(value, str):
                    _input_texts[cid] = value[:MAX_INPUT_TEXT]
                elif value is None or isinstance(value, (bool, int, float)):
                    _input_texts[cid] = json.dumps(value)
                else:
                    _input_texts[cid] = None
            item["text"] = _input_texts.get(cid)


MAX_JOBS_BY_ID = 200
"""The most jobs one ``jobSummariesByIds`` may ask for."""


async def job_summaries_by_ids(reader: ViewerReader, job_ids: Sequence[JobID]) -> list[JobSummary]:
    """The summaries of the jobs named, in the order named, leaving out any the project does not
    have or the reader's scope does not allow: what the job picker's commit view reads for the
    jobs on screen."""
    if len(job_ids) > MAX_JOBS_BY_ID:
        raise ValueError(f"at most {MAX_JOBS_BY_ID} jobs per request")
    allowed = [job_id for job_id in job_ids if reader.scope.allows(job_id)]
    async with reader.database.transaction(snapshot=True) as tx:
        summaries = await job_summaries(tx, allowed, reader.scope)
    found = [summaries[job_id] for job_id in allowed if job_id in summaries]
    await input_texts(reader, found)
    return found


async def job_reuse(reader: ViewerReader, job_id: JobID) -> list[NodeReuse]:
    """For each run node of the job's root workflow, how many of the step results the job uses
    under it (in the node's own steps, or anywhere inside the child workflows it started) the job
    ran itself, and how many it took from the cache, which another job had run. A step with no
    result yet counts as neither. A job outside the reader's scope has no nodes here, as a job
    the database does not have."""
    if not reader.scope.allows(job_id):
        return []
    root = (
        select(workflow_invocations.c.id)
        .join(jobs, jobs.c.id == workflow_invocations.c.job_id)
        .where(jobs.c.id == job_id, workflow_invocations.c.pathname == jobs.c.root)
        .scalar_subquery()
    )
    under = (
        select(
            spawns.c.node_identity.label("top"),
            spawns.c.child_invocation_id.label("invocation"),
            spawns.c.step_cache_key.label("key"),
        )
        .where(spawns.c.invocation_id == root)
        .cte("under", recursive=True)
    )
    inner = spawns.alias("inner_spawn")
    under = under.union_all(
        select(under.c.top, inner.c.child_invocation_id, inner.c.step_cache_key).where(
            inner.c.invocation_id == under.c.invocation
        )
    )
    ours = job_attempt_provenance.c.job_id == job_id
    async with reader.database.transaction(snapshot=True) as tx:
        rows = (
            await tx.execute(
                select(
                    under.c.top,
                    func.count().filter(ours).label("ran"),
                    func.count().filter(~ours).label("reused"),
                )
                .select_from(
                    under.join(
                        job_step_bindings,
                        and_(
                            job_step_bindings.c.job_id == job_id,
                            job_step_bindings.c.cache_key == under.c.key,
                        ),
                    )
                    .join(step_results, step_results.c.id == job_step_bindings.c.step_result_id)
                    .join(
                        job_attempt_provenance,
                        job_attempt_provenance.c.id == step_results.c.job_attempt_id,
                    )
                )
                .group_by(under.c.top)
            )
        ).all()
    return [{"identity": cid_to_str(row.top), "ran": row.ran, "reused": row.reused} for row in rows]


async def job_page(
    reader: ViewerReader,
    first: int,
    after: str | None,
    search: str = "",
    workflow: str | None = None,
) -> JobPage:
    """The jobs the reader's scope allows that match ``search`` (in their workflow's name or
    their id), of ``workflow`` alone when it is given, newest first."""
    cursor_scope = ["jobs", search, workflow or ""]
    last = position(after, cursor_scope)
    page_size(first)
    async with reader.database.transaction(snapshot=True) as tx:
        query = select(jobs).where(reader.scope.restrict(jobs.c.id))
        if last is not None:
            created_at, job_id = datetime.fromisoformat(last[0]), uuid.UUID(last[1])
            query = query.where(
                or_(
                    jobs.c.created_at < created_at,
                    and_(jobs.c.created_at == created_at, jobs.c.id > job_id),
                )
            )
        if workflow is not None:
            query = query.where(jobs.c.workflow_name == workflow)
        if search:
            needle = (
                "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            query = query.where(
                or_(jobs.c.workflow_name.ilike(needle), cast(jobs.c.id, Text).ilike(needle))
            )
        records = (
            await tx.execute(query.order_by(jobs.c.created_at.desc(), jobs.c.id).limit(first + 1))
        ).all()
        summaries = await job_summaries(tx, [record.id for record in records[:first]], reader.scope)
        items = [summaries[record.id] for record in records[:first]]
    await input_texts(reader, items)
    return {
        "items": items,
        "next": cursor(
            cursor_scope, [records[first - 1].created_at.isoformat(), str(records[first - 1].id)]
        )
        if len(records) > first
        else None,
    }


async def job_workflows(reader: ViewerReader) -> list[str]:
    """The names of the workflows that the jobs the reader's scope allows are of, in order."""
    async with reader.database.transaction(snapshot=True) as tx:
        rows = await tx.execute(
            select(jobs.c.workflow_name)
            .where(reader.scope.restrict(jobs.c.id))
            .distinct()
            .order_by(jobs.c.workflow_name)
        )
        return [row.workflow_name for row in rows]


JOB_COMMITS_LIMIT = 2000
"""The most jobs, newest first, the commit view places: enough for a project's working history
while keeping the page's one read of it small."""


async def job_launches(
    reader: ViewerReader,
    code_versions: dict[bytes, CodeVersion],
    limit: int = JOB_COMMITS_LIMIT,
) -> list[Launch]:
    """The newest ``limit`` jobs the reader's scope allows, with the code each launched at (its
    first job attempt's), newest first, in one query plus a load of the code versions not in
    ``code_versions`` yet. The local viewer's commit view reads this; no route of the reader
    app does.

    ``code_versions`` is the caller's, kept between calls: a code version is content-addressed,
    so one read never goes stale. A job with no job attempt is left out.
    """
    first = job_attempt_provenance.alias("first_job_attempt")
    launched_at = (
        select(first.c.code_version_cid)
        .where(first.c.job_id == jobs.c.id)
        .order_by(first.c.created_at, first.c.id)
        .limit(1)
        .scalar_subquery()
    )
    async with reader.database.transaction(snapshot=True) as tx:
        rows = (
            await tx.execute(
                select(
                    jobs.c.id, jobs.c.workflow_name, jobs.c.created_at, launched_at.label("code")
                )
                .where(reader.scope.restrict(jobs.c.id))
                .order_by(jobs.c.created_at.desc(), jobs.c.id)
                .limit(limit)
            )
        ).all()
    for cid in {bytes(row.code) for row in rows if row.code is not None} - code_versions.keys():
        code_versions[cid] = await reader.entities.load(EntityID(cid=cid), as_type=CodeVersion)
    return [
        Launch(
            job_id=str(row.id),
            workflow_name=row.workflow_name,
            created_at=row.created_at,
            commit=code_versions[bytes(row.code)].commit,
            tree=code_versions[bytes(row.code)].tree,
            ephemeral=code_versions[bytes(row.code)].ephemeral,
            project_path=code_versions[bytes(row.code)].project_path,
        )
        for row in rows
        if row.code is not None
    ]


async def level_overview(
    database: Database, job_id: JobID, pathname: str, scope: JobScope = JobScope.all()
) -> LevelOverview | None:
    if not scope.allows(job_id):
        return None
    async with database.transaction(snapshot=True) as tx:
        states = StatusQueries([job_id])
        inv = (
            await tx.execute(
                select(states.invocations).where(states.invocations.c.pathname == pathname)
            )
        ).first()
        if inv is None:
            return None
        records = await ledger.load_nodes(tx, inv.id)
        counts, activity, failures = await run_statuses(
            tx, job_id, states.spawn_query(inv.id).subquery()
        )
        attempt_count = (
            await tx.execute(
                select(func.count()).select_from(attempts).where(attempts.c.invocation_id == inv.id)
            )
        ).scalar_one()
        latest_row = (
            await tx.execute(
                select(attempts.c.id)
                .where(attempts.c.invocation_id == inv.id)
                .order_by(attempts.c.started_at.desc(), attempts.c.id.desc())
                .limit(1)
            )
        ).first()
        latest = (
            None
            if latest_row is None
            else await execution.get_attempt(tx, AttemptID(str(latest_row.id)))
        )
        awaited = None
        if latest is not None and latest.live:
            event = (
                await tx.execute(
                    select(attempt_events)
                    .where(attempt_events.c.attempt_id == latest.id)
                    .order_by(attempt_events.c.seq.desc())
                    .limit(1)
                )
            ).first()
            if event is not None and event.kind == "observe":
                awaited = cid_to_str(event.identity)
        # The code the latest attempt ran, which the viewer's code links and docs follow.
        code_version = (
            None
            if latest is None
            else (
                await ledger.get_job_attempt_provenance(tx, latest.owner_job_attempt_id)
            ).code_version.entity_id.cid_str()
        )
        run_versions = await read_run_versions(tx, job_id, inv.id)
        result: LevelOverview = {
            "pathname": pathname,
            "workflowName": inv.workflow_name,
            "state": inv.display_state,
            "resultCid": None if inv.result_cid is None else cid_to_str(inv.result_cid),
            "error": error_view(latest.error)
            if latest is not None and inv.display_state == "failed"
            else None,
            "startedAt": None if latest is None else latest.started_at,
            "finishedAt": None
            if latest is None or latest.finished_at is None
            else latest.finished_at,
            "nodes": [
                overview_node(
                    record,
                    counts.get(record.identity.cid_str()),
                    activity.get(record.identity.cid_str()),
                    failures.get(record.identity.cid_str()),
                    run_versions.get(record.identity.cid, ""),
                )
                for record in records
            ],
            "attemptCount": attempt_count,
            "codeVersion": code_version,
            "returnedNode": None if inv.returned_node is None else cid_to_str(inv.returned_node),
            "awaitingNode": awaited,
            "version": await read_invocation_version(
                tx, job_id, inv.id, finished=inv.result_cid is not None
            ),
            "asOf": (await tx.execute(select(func.now()))).scalar_one(),
        }
    return result


async def run_statuses(
    tx: Transaction, job_id: JobID, targets: Any
) -> tuple[dict[str, StateCounts], dict[str, RunActivity], dict[str, list[FailureGroup]]]:
    """Each run node's counts by state, its timing, and its failures grouped by error type, from
    its spawns (``targets``, a ``spawn_query`` subquery) and the attempts behind them.

    The spawn rows are costly to compute, so they are read in one query, grouped finely enough for
    all three: by state, and for failures by error type. Each group's timing aggregates (earliest,
    latest, how many) combine exactly into the node's."""
    attempt = attempts.alias("spawn_attempt")
    owner = job_attempts.alias("spawn_job_attempt")
    joined = targets.outerjoin(attempt, attempt.c.id == targets.c.attempt_id).outerjoin(
        owner, and_(owner.c.id == attempt.c.owner_job_attempt_id, owner.c.job_id == job_id)
    )
    ours = owner.c.id.is_not(None)
    finished = and_(ours, targets.c.state.in_(["completed", "failed"]))
    failed_type = case((targets.c.state == "failed", attempt.c.error["type_name"].astext))
    rows = (
        await tx.execute(
            select(
                targets.c.node_identity,
                targets.c.state,
                failed_type.label("failed_type"),
                func.count().label("count"),
                func.min(attempt.c.error["message"].astext).label("message"),
                func.min(case((ours, attempt.c.started_at))).label("first_started"),
                func.max(case((finished, attempt.c.finished_at))).label("last_finished"),
                func.count(case((and_(finished, attempt.c.finished_at.is_not(None)), 1))).label(
                    "finished"
                ),
                func.min(case((targets.c.state == "running", attempt.c.started_at))).label(
                    "oldest_running"
                ),
            )
            .select_from(joined)
            .group_by(targets.c.node_identity, targets.c.state, failed_type)
        )
    ).all()
    counts: dict[str, StateCounts] = {}
    activity: dict[str, RunActivity] = {}
    failures: dict[str, list[FailureGroup]] = {}
    for row in rows:
        node = cid_to_str(row.node_identity)
        node_counts = counts.setdefault(node, state_counts())
        node_counts[row.state] += row._mapping["count"]
        timing = activity.setdefault(node, no_activity())
        timing["firstStartedAt"] = _earliest(timing["firstStartedAt"], row.first_started)
        timing["lastFinishedAt"] = _latest(timing["lastFinishedAt"], row.last_finished)
        timing["finished"] += row.finished
        timing["oldestRunningSince"] = _earliest(timing["oldestRunningSince"], row.oldest_running)
        if row.failed_type is not None:
            failures.setdefault(node, []).append(
                {
                    "typeName": row.failed_type,
                    "message": row.message or "",
                    "count": row._mapping["count"],
                }
            )
    for groups in failures.values():
        groups.sort(key=lambda group: (-group["count"], group["typeName"]))
    return counts, activity, failures


def _earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    return b if a is None else a if b is None else min(a, b)


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    return b if a is None else a if b is None else max(a, b)


async def run_context(
    tx: Transaction, job_id: JobID, pathname: str, node_id: str, scope: JobScope = JobScope.all()
):
    """The spawns of a run node as a subquery, the node's dims, and its type.

    Raises:
        NotFoundError: If the job has no invocation at ``pathname``, or the scope does not
            allow the job (the same error either way).
    """
    require_invocation(scope, job_id, pathname)
    invocation = await ledger.find_invocation(tx, job_id, pathname)
    node = (
        await tx.execute(
            select(nodes).where(
                nodes.c.invocation_id == invocation.id,
                nodes.c.identity == cid_from_str(node_id),
            )
        )
    ).one()
    if node.kind not in ("run_step", "run_child_workflow"):
        raise ValueError("node is not a run")
    dims = [*node.params.get("map_over", []), *node.params.get("replicas", {})]
    states = StatusQueries([job_id])
    return states.spawn_query(invocation.id, node.identity).subquery(), dims, node.type


def selected(query: Any, key: Any, dims: list[str], selection: dict[str, Any]):
    for name, value in selection.items():
        if name not in dims or type(value) not in (str, int):
            raise ValueError("invalid dimension selection")
        query = query.where(key[dims.index(name)] == literal(value, type_=JSONB))
    return query


async def run_page(
    database: Database,
    job_id: JobID,
    pathname: str,
    node_id: str,
    selection: dict[str, Any],
    first: int,
    after: str | None,
    state: str | None = None,
    unfinished: bool = False,
    offset: int | None = None,
    scope: JobScope = JobScope.all(),
) -> RunPage:
    """A page of the run's invocations the selection picks out, in key order: the first, the
    ones after a cursor, or, by `offset`, the page holding that row (the last page for an offset
    past the end)."""
    page_size(first)
    if state and unfinished:
        raise ValueError("a run page filters on a state or on unfinished, not both")
    if offset is not None and (type(offset) is not int or offset < 0 or after is not None):
        raise ValueError("invalid run offset; use either an offset or a cursor")
    cursor_scope = [str(job_id), pathname, node_id, selection, state, unfinished]
    last = position(after, cursor_scope)
    async with database.transaction(snapshot=True) as tx:
        targets, dims, _ = await run_context(tx, job_id, pathname, node_id, scope)
        query = selected(select(targets), targets.c.key, dims, selection)
        if state:
            query = query.where(targets.c.state == state)
        if unfinished:
            query = query.where(targets.c.state != "completed")
        total = (await tx.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        if last is not None:
            query = query.where(targets.c.key > literal(last, type_=JSONB))
        query = query.order_by(targets.c.key)
        if offset is not None:
            offset = page_offset(offset, first, total)
            query = query.offset(offset)
        rows = (await tx.execute(query.limit(first + 1))).all()
    return {
        "items": [spawn_dict(row) for row in rows[:first]],
        "total": total,
        "offset": offset,
        "next": cursor(cursor_scope, rows[first - 1].key) if len(rows) > first else None,
    }


async def key_page(
    database: Database,
    job_id: JobID,
    pathname: str,
    node_id: str,
    dim: str,
    selection: dict[str, Any],
    search: str,
    first: int,
    after: str | None,
    offset: int | None = None,
    scope: JobScope = JobScope.all(),
) -> KeyPage:
    page_size(first, KEY_LIST_LIMIT)
    if offset is not None and (type(offset) is not int or offset < 0 or after is not None):
        raise ValueError("invalid key offset; use either an offset or a cursor")
    # Other selections affect compatibility, not which values occupy this page.
    cursor_scope = [str(job_id), pathname, node_id, dim, search]
    last = position(after, cursor_scope)
    async with database.transaction(snapshot=True) as tx:
        targets, dims, _ = await run_context(tx, job_id, pathname, node_id, scope)
        if dim not in dims:
            raise ValueError("unknown dimension")
        key = targets.c.key[dims.index(dim)]
        matching = selected(
            select(targets.c.key),
            targets.c.key,
            dims,
            {k: v for k, v in selection.items() if k != dim},
        ).whereclause
        values = select(key.label("value")).distinct()
        if search:
            values = values.where(func.strpos(func.lower(cast(key, Text)), search.lower()) > 0)
        total = (await tx.execute(select(func.count()).select_from(values.subquery()))).scalar_one()
        if offset is not None:
            offset = min(offset // first * first, max(0, (total - 1) // first * first))
            values = values.offset(offset)
        if last is not None:
            values = values.where(key > literal(last, type_=JSONB))
        page = (await tx.execute(values.order_by(key).limit(first + 1))).scalars().all()
        rows = (
            (
                await tx.execute(
                    select(
                        key.label("value"),
                        targets.c.state,
                        func.count().label("count"),
                        func.count()
                        .filter(matching if matching is not None else true())
                        .label("matching_count"),
                    )
                    .where(key.in_([literal(value, type_=JSONB) for value in page[:first]]))
                    .group_by(key, targets.c.state)
                )
            ).all()
            if page
            else []
        )
    counts = {json.dumps(value): {} for value in page[:first]}
    matching_counts = {json.dumps(value): {} for value in page[:first]}
    for row in rows:
        counts[json.dumps(row.value)][row.state] = row._mapping["count"]
        if row.matching_count:
            matching_counts[json.dumps(row.value)][row.state] = row.matching_count
    return {
        "items": [
            {
                "value": value,
                "compatible": bool(matching_counts[json.dumps(value)]),
                "counts": state_counts()
                | (matching_counts[json.dumps(value)] or counts[json.dumps(value)]),
            }
            for value in page[:first]
        ],
        "total": total,
        "offset": offset,
        "next": cursor(cursor_scope, page[first - 1]) if len(page) > first else None,
    }


async def entity_meta(database: Database, cid: str) -> EntityMeta | None:
    async with database.transaction() as tx:
        row = (
            await tx.execute(select(entities.c.type_tag).where(entities.c.cid == cid_from_str(cid)))
        ).first()
    return None if row is None else {"cid": cid, "typeTag": row.type_tag}
