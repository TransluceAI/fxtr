"""Payload-free viewer reads and bounded navigation over the runner's relational records.

Status comes from the runner's canonical status relations. These readers load no entity bytes
or logs for summaries and navigation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Text, and_, case, cast, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB

from fxtr.core.entity_data import cid_from_str, cid_to_str
from fxtr.runner.postgres import execution, ledger
from fxtr.runner.postgres.client import LocalProjectClient
from fxtr.runner.postgres.database import Transaction
from fxtr.runner.postgres.ledger import JobID
from fxtr.runner.postgres.schema import (
    attempt_events,
    attempts,
    entities,
    job_attempts,
    jobs,
    nodes,
    workflow_invocations,
)
from fxtr.runner.postgres.status_queries import StatusQueries
from fxtr.runner.session import AttemptID

from .contracts import (
    EntityMeta,
    FailureGroup,
    JobPage,
    JobSummary,
    KeyPage,
    LevelOverview,
    RunActivity,
    RunPage,
    SpawnView,
    StateCounts,
    state_counts,
)
from .projections import error_view, no_activity, overview_node
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


async def job_summaries(tx: Transaction, job_ids: Sequence[JobID]) -> dict[JobID, JobSummary]:
    """One grouped status query for the requested jobs, regardless of page or history size."""
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
    return {
        row.id: {
            "id": str(row.id),
            "workflowName": row.workflow_name,
            "root": row.root,
            "createdAt": row.created_at,
            "state": row.state,
            "failed": row.failed,
        }
        for row in rows
    }


async def job_page(
    client: LocalProjectClient, first: int, after: str | None, search: str = ""
) -> JobPage:
    scope = ["jobs", search]
    last = position(after, scope)
    page_size(first)
    async with client.database.transaction(snapshot=True) as tx:
        query = select(jobs)
        if last is not None:
            created_at, job_id = datetime.fromisoformat(last[0]), uuid.UUID(last[1])
            query = query.where(
                or_(
                    jobs.c.created_at < created_at,
                    and_(jobs.c.created_at == created_at, jobs.c.id > job_id),
                )
            )
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
        summaries = await job_summaries(tx, [record.id for record in records[:first]])
        items = [summaries[record.id] for record in records[:first]]
    return {
        "items": items,
        "next": cursor(
            scope, [records[first - 1].created_at.isoformat(), str(records[first - 1].id)]
        )
        if len(records) > first
        else None,
    }


async def level_overview(
    client: LocalProjectClient, job_id: JobID, pathname: str
) -> LevelOverview | None:
    async with client.database.transaction(snapshot=True) as tx:
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


async def run_context(tx: Transaction, job_id: JobID, pathname: str, node_id: str):
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
    client: LocalProjectClient,
    job_id: JobID,
    pathname: str,
    node_id: str,
    selection: dict[str, Any],
    first: int,
    after: str | None,
    state: str | None = None,
    unfinished: bool = False,
    offset: int | None = None,
) -> RunPage:
    """A page of the run's invocations the selection picks out, in key order: the first, the
    ones after a cursor, or, by `offset`, the page holding that row (the last page for an offset
    past the end)."""
    page_size(first)
    if state and unfinished:
        raise ValueError("a run page filters on a state or on unfinished, not both")
    if offset is not None and (type(offset) is not int or offset < 0 or after is not None):
        raise ValueError("invalid run offset; use either an offset or a cursor")
    scope = [str(job_id), pathname, node_id, selection, state, unfinished]
    last = position(after, scope)
    async with client.database.transaction(snapshot=True) as tx:
        targets, dims, _ = await run_context(tx, job_id, pathname, node_id)
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
        "next": cursor(scope, rows[first - 1].key) if len(rows) > first else None,
    }


async def key_page(
    client: LocalProjectClient,
    job_id: JobID,
    pathname: str,
    node_id: str,
    dim: str,
    selection: dict[str, Any],
    search: str,
    first: int,
    after: str | None,
    offset: int | None = None,
) -> KeyPage:
    page_size(first, KEY_LIST_LIMIT)
    if offset is not None and (type(offset) is not int or offset < 0 or after is not None):
        raise ValueError("invalid key offset; use either an offset or a cursor")
    # Other selections affect compatibility, not which values occupy this page.
    scope = [str(job_id), pathname, node_id, dim, search]
    last = position(after, scope)
    async with client.database.transaction(snapshot=True) as tx:
        targets, dims, _ = await run_context(tx, job_id, pathname, node_id)
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
        "next": cursor(scope, page[first - 1]) if len(page) > first else None,
    }


async def entity_meta(client: LocalProjectClient, cid: str) -> EntityMeta | None:
    async with client.database.transaction() as tx:
        row = (
            await tx.execute(select(entities.c.type_tag).where(entities.c.cid == cid_from_str(cid)))
        ).first()
    return None if row is None else {"cid": cid, "typeTag": row.type_tag}
