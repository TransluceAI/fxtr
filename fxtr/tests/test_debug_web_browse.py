"""Viewer status parity and payload/query bounds over large mapped runs."""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import replace
from typing import Any

import pytest
from frozendict import frozendict
from postgres_testing import (
    OWNER,
    a_client,
    a_code_version,
    a_job,
    a_request,
    assert_policy_agrees,
    expand_children,
    request,
    running_root,
    set_attempt_state,
    set_job_attempt_state,
    start_invocation,
    start_root,
    start_step,
    take,
)

pytest.importorskip("sqlalchemy")

from sqlalchemy import event, select, text  # noqa: E402

from fxtr.core.entity_data import cid_to_str  # noqa: E402
from fxtr.entity_defns.array import Array, ArrayType, Key  # noqa: E402
from fxtr.runner.nodes import RunStep, SourceArray  # noqa: E402
from fxtr.runner.postgres import execution, ledger  # noqa: E402
from fxtr.runner.postgres.client import LocalProjectClient  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.debug_web.browse import (  # noqa: E402
    job_page,
    job_summaries,
    key_page,
    level_overview,
    run_page,
)
from fxtr.runner.postgres.debug_web.contracts import OutputSource, state_counts  # noqa: E402
from fxtr.runner.postgres.entities import PostgresEntityStorage  # noqa: E402
from fxtr.runner.postgres.ledger import Emission  # noqa: E402
from fxtr.runner.postgres.schema import (  # noqa: E402
    attempts,
    job_attempt_provenance,
    metadata,
    nodes,
    spawns,
    workflow_invocations,
)
from fxtr.runner.postgres.status_queries import StatusQueries  # noqa: E402
from fxtr.runner.session import AttemptError  # noqa: E402

pytestmark = pytest.mark.anyio


def producer(job: Any) -> Any:
    """One of the job's job attempts, to stand as the producer of a result a test sets directly."""
    return (
        select(job_attempt_provenance.c.id)
        .where(job_attempt_provenance.c.job_id == job)
        .limit(1)
        .scalar_subquery()
    )


async def test_run_revisions_detect_swapped_targets_without_refreshing_unrelated_runs(
    database: Database,
):
    async with a_client(database) as client:
        job = await a_job(database)
        root = await start_root(database, job)
        run, children = await expand_children(database, root)
        other_run = replace(run, name="other", cache_key_prefix="root/other")
        async with database.transaction() as tx:
            act = await execution.guard(tx, root.attempt_id)
            await ledger.record_emissions(tx, act, [ledger.Emission(other_run, other_run.type)])
            await ledger.expand_run_node(
                tx,
                act,
                other_run.content_id(),
                [
                    ledger.ChildSpec(
                        key=(0,),
                        pathname="root/other[k:0]",
                        workflow_name="child_wf",
                        cache_key_prefix="root/other[k:0]",
                        inputs={},
                    )
                ],
            )

        def node(overview, identity):
            return next(node for node in overview["nodes"] if node["identity"] == identity)

        run_id, other_id = run.content_id().cid_str(), other_run.content_id().cid_str()
        before = await level_overview(client, job, "root")
        await start_invocation(database, job, children[0])
        after = await level_overview(client, job, "root")
        assert before is not None and after is not None
        assert after["version"] != before["version"]
        assert node(after, run_id)["version"] != node(before, run_id)["version"]
        assert node(after, other_id)["version"] == node(before, other_id)["version"]

        snapshots = []
        pages = []
        for child in children:
            await client.jobs.cancel(job, hard=True)
            await client.jobs.resume(job, owner=OWNER, code_version=await a_code_version(database))
            await start_invocation(database, job, child)
            snapshots.append(await level_overview(client, job, "root"))
            pages.append(await run_page(client, job, "root", run_id, {}, 25, None))
        left, right = snapshots
        assert left is not None and right is not None
        # Each resume's job attempt is due to run every unfinished invocation it has not tried.
        assert [item["state"] for item in pages[0]["items"]] == ["running", "queued"]
        assert [item["state"] for item in pages[1]["items"]] == ["queued", "running"]
        assert node(left, run_id)["counts"] == node(right, run_id)["counts"]
        assert node(left, run_id)["version"] != node(right, run_id)["version"]
        assert left["version"] != right["version"]
        again = await level_overview(client, job, "root")
        assert again is not None and again["asOf"] >= right["asOf"]
        assert {**again, "asOf": None} == {**right, "asOf": None}


async def test_overview_reports_run_activity_and_failures_by_type(database: Database):
    async with a_client(database) as client:
        job = await a_job(database)
        root = await start_root(database, job)
        run, children = await expand_children(database, root)
        run_id = run.content_id().cid_str()

        async def run_node():
            overview = await level_overview(client, job, "root")
            assert overview is not None
            node = next(node for node in overview["nodes"] if node["identity"] == run_id)
            assert node["counts"] is not None and node["activity"] is not None
            return overview, node, node["counts"], node["activity"]

        overview, node, _, activity = await run_node()
        assert activity == {
            "firstStartedAt": None,
            "lastFinishedAt": None,
            "finished": 0,
            "oldestRunningSince": None,
        }
        assert node["failures"] == []
        leaves = [node for node in overview["nodes"] if node["counts"] is None]
        assert leaves and all(n["activity"] is None and n["failures"] is None for n in leaves)

        first = await start_invocation(database, job, children[0])
        _, _, _, activity = await run_node()
        started = activity["oldestRunningSince"]
        assert started is not None and activity["finished"] == 0

        async with database.transaction() as tx:
            await tx.execute(
                attempts.update()
                .where(attempts.c.id == uuid.UUID(first.attempt_id))
                .values(
                    state="failed",
                    finished_at=text("now()"),
                    error={"type_name": "RuntimeError", "message": "boom", "traceback": ""},
                )
            )
        await start_invocation(database, job, children[1])
        overview, node, counts, activity = await run_node()
        assert counts["failed"] == 1 and counts["running"] == 1
        assert node["failures"] == [{"typeName": "RuntimeError", "message": "boom", "count": 1}]
        assert activity["finished"] == 1 and activity["firstStartedAt"] == started
        last_finished, oldest_running = activity["lastFinishedAt"], activity["oldestRunningSince"]
        assert last_finished is not None and last_finished >= started
        assert oldest_running is not None and oldest_running >= started
        assert overview["asOf"] >= last_finished


async def test_job_page_batches_mixed_statuses_and_single_reads_ignore_attempt_history(
    database: Database,
):
    async with a_client(database) as client:
        job, root = await running_root(database)
        same = await a_request(database, 1)
        await request(database, job, root, "shared", same)
        step = await start_step(database, job, "shared")
        assert step is not None
        async with database.transaction() as tx:
            act = await execution.guard_step(tx, step.attempt.id, ending=True)
            await execution.close_step_attempt(
                tx,
                act,
                "failed",
                error=AttemptError(type_name="RuntimeError", message="boom", traceback=""),
            )
        other, other_root = await running_root(database)
        await request(database, other, other_root, "shared", same)
        for _ in range(23):
            await a_job(database)
        await set_attempt_state(database, root, "interrupted")
        await set_job_attempt_state(database, job, "cancelled")
        statements = []

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        try:
            page = await job_page(client, 25, None)
            assert len(statements) == 3
            assert len(page["items"]) == 25
            summaries = {item["id"]: item for item in page["items"]}
            assert summaries[str(job)]["state"] == "stopped" and summaries[str(job)]["failed"]
            assert (
                summaries[str(other)]["state"] == "running" and not summaries[str(other)]["failed"]
            )
            async with database.transaction(snapshot=True) as tx:
                statements.clear()
                before = await execution.step_status(tx, other, "shared")
                baseline = len(statements)
            salt = str(uuid.uuid4())
            async with database.transaction() as tx:
                await tx.execute(
                    text("""
                    INSERT INTO job_attempt_provenance (id, job_id, created_at, code_version_cid)
                    SELECT md5(:salt || i::text)::uuid, :job,
                        '2000-01-01'::timestamptz + i * interval '1 second',
                        (SELECT code_version_cid FROM job_attempt_provenance
                            WHERE job_id = :job LIMIT 1)
                    FROM generate_series(1, 100) i
                """),
                    {"salt": salt, "job": other},
                )
                await tx.execute(
                    text("""
                    INSERT INTO job_attempts (id, job_id, state, owner, settled_at)
                    SELECT md5(:salt || i::text)::uuid, :job, 'cancelled', 'test', now()
                    FROM generate_series(1, 100) i
                """),
                    {"salt": salt, "job": other},
                )
            async with database.transaction(snapshot=True) as tx:
                statements.clear()
                assert await execution.step_status(tx, other, "shared") == before
                assert len(statements) == baseline == 1
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", record)


async def parity(client: LocalProjectClient, job: ledger.JobID):
    expected = await client.jobs.status(job)
    async with client.database.transaction(snapshot=True) as tx:
        states = StatusQueries([job])
        invocations = (await tx.execute(select(states.invocations))).all()
        steps = (await tx.execute(select(states.steps))).all()
        summary = (await job_summaries(tx, [job]))[job]
    assert {row.pathname: row.display_state for row in invocations} == {
        path: state.state for path, state in expected.invocations.items()
    }
    assert {row.cache_key: row.display_state for row in steps} == {
        key: state.state for key, state in expected.steps.items()
    }
    assert summary["state"] == expected.state
    return expected


async def test_step_projection_matches_failures_conflicts_ready_and_bound(database: Database):
    async with a_client(database) as client:
        job_a, root_a = await running_root(database)
        job_b, root_b = await running_root(database)
        job_c, root_c = await running_root(database)
        same = await a_request(database, 1)
        different = await a_request(database, 2)
        await request(database, job_a, root_a, "shared", same)
        assert (await parity(client, job_a)).steps["shared"].state == "queued"
        step_a = await start_step(database, job_a, "shared")
        assert step_a is not None
        assert (await parity(client, job_a)).steps["shared"].state == "running"
        async with database.transaction() as tx:
            act = await execution.guard_step(tx, step_a.attempt.id, ending=True)
            await execution.close_step_attempt(
                tx,
                act,
                "failed",
                error=AttemptError(type_name="RuntimeError", message="boom", traceback=""),
            )
        await request(database, job_b, root_b, "shared", same)
        step_b = await start_step(database, job_b, "shared")
        assert step_b is not None
        await request(database, job_c, root_c, "shared", different)
        assert (await parity(client, job_a)).steps["shared"].state == "failed"
        assert (await parity(client, job_c)).steps["shared"].state == "conflict"
        result = await client.entities.store(Array.scalar(42, int))
        async with database.transaction() as tx:
            act = await execution.guard_step(tx, step_b.attempt.id, ending=True)
            await client.entities.flush(tx)
            await execution.complete_step(tx, act, result)
        assert (await parity(client, job_b)).steps["shared"].state == "running"  # ready to bind
        await take(database, root_b, "shared")
        assert (await parity(client, job_b)).steps["shared"].state == "completed"
        assert (await parity(client, job_a)).steps["shared"].state == "failed"
        await set_job_attempt_state(database, job_a, "cancelled")
        await set_job_attempt_state(database, job_c, "cancelled")
        # Without an active job attempt, a job still shows what stopped it.
        assert (await parity(client, job_a)).steps["shared"].state == "failed"
        assert (await parity(client, job_c)).steps["shared"].state == "conflict"


async def test_invocation_projection_follows_resume(database: Database):
    """A stopped job shows what stopped each invocation; a resume's job attempt is due to run
    every unfinished invocation of the job, whatever earlier job attempts' attempts ended as."""
    async with a_client(database) as client:
        job = await a_job(database)
        root = await start_root(database, job)
        _, children = await expand_children(database, root)
        first, second = (child.pathname for child in children)
        assert (await parity(client, job)).invocations[first].state == "queued"
        child = await start_invocation(database, job, children[0])
        await set_attempt_state(database, child.attempt_id, "abandoned")
        await parity(client, job)
        await set_attempt_state(database, child.attempt_id, "failed")
        await set_attempt_state(database, root.attempt_id, "interrupted")
        await set_job_attempt_state(database, job, "cancelled")
        stopped = await parity(client, job)
        assert stopped.invocations[first].state == "failed"
        assert stopped.invocations[second].state == "stopped"
        assert stopped.invocations["root"].state == "interrupted"
        await assert_policy_agrees(database, job)
        async with database.transaction() as tx:
            await execution.resume(
                tx, job, owner=OWNER, code_version=await a_code_version(database)
            )
        resumed = await parity(client, job)
        assert {path: s.state for path, s in resumed.invocations.items()} == {
            "root": "queued",
            first: "queued",
            second: "queued",
        }
        await assert_policy_agrees(database, job)


# Builds and queries a 100,000-spawn job: about 4s on a laptop, and several times that on CI.
@pytest.mark.timeout_seconds(30)
async def test_overview_and_key_pages_at_100000_spawns(database: Database):
    async with a_client(database) as client:
        job = await a_job(database)
        root = await start_root(database, job)
        run, _ = await expand_children(database, root)
        statements: list[str] = []

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        try:
            statements.clear()
            await level_overview(client, job, "root")
            baseline_queries = len(statements)
            # Bulk seed real relational targets without spending 100k runner executions on the
            # fixture.
            async with database.transaction() as tx:
                await tx.execute(
                    text("""
                    INSERT INTO workflow_invocations
                        (id, job_id, pathname, workflow_name, cache_key_prefix)
                    SELECT md5(:salt || i::text)::uuid, :job, 'child-' || i, 'child_wf',
                        'child-' || i
                    FROM generate_series(2, 99999) i
                """),
                    {"salt": str(uuid.uuid4()), "job": job},
                )
                await tx.execute(
                    text("""
                    INSERT INTO spawns(invocation_id, node_identity, key, child_invocation_id)
                    SELECT :root, :node, jsonb_build_array(substring(pathname from 7)::int), id
                    FROM workflow_invocations WHERE job_id=:job AND pathname LIKE 'child-%'
                """),
                    {"root": root.invocation.id, "node": run.content_id().cid, "job": job},
                )
            statements.clear()
            started = time.monotonic()
            overview = await level_overview(client, job, "root")
            elapsed = time.monotonic() - started
            assert len(statements) == baseline_queries
            assert overview is not None
            run_node = next(node for node in overview["nodes"] if node["counts"] is not None)
            assert run_node["counts"] == state_counts() | {"queued": 100_000}
            assert "spawns" not in overview and "attempts" not in overview
            assert len(json.dumps(overview, default=str)) < 10_000
            assert not any(
                "entities.data" in query or "attempt_logs.message" in query for query in statements
            )
            # Full status also batches this many targets without exceeding the driver's bind limit.
            full_status = await client.jobs.status(job)
            assert len(full_status.invocations) == 100_001
            assert full_status.invocations["child-99999"].state == "queued"
            node = run_node["identity"]
            keys = await key_page(client, job, "root", node, "k", {}, "", 50, None)
            assert len(keys["items"]) == 50 and keys["total"] == 100_000
            # What the viewer's key list reads: the first KEY_LIST_LIMIT values, and whether more.
            listed = await key_page(client, job, "root", node, "k", {}, "", 1000, None)
            assert [item["value"] for item in listed["items"]] == list(range(1000))
            assert listed["next"] is not None
            with pytest.raises(ValueError, match="between 1 and 1000"):
                await key_page(client, job, "root", node, "k", {}, "", 1001, None)
            for offset in [99_980, 100_000]:
                jumped = await key_page(client, job, "root", node, "k", {}, "", 25, None, offset)
                assert jumped["offset"] == 99_975 and jumped["next"] is None
                assert [item["value"] for item in jumped["items"]] == list(range(99_975, 100_000))
            empty = await key_page(client, job, "root", node, "k", {}, "absent", 25, None, 50)
            assert empty["offset"] == 0 and empty["items"] == [] and empty["total"] == 0
            page = await run_page(client, job, "root", node, {"k": 99999}, 50, None)
            assert page["total"] == 1 and page["items"][0]["key"] == [99999]
            for offset in [99_980, 100_000]:
                jumped = await run_page(client, job, "root", node, {}, 25, None, offset=offset)
                assert jumped["offset"] == 99_975 and jumped["next"] is None
                assert [item["key"] for item in jumped["items"]] == [
                    [k] for k in range(99_975, 100_000)
                ]
            with pytest.raises(ValueError, match="either an offset or a cursor"):
                await run_page(client, job, "root", node, {}, 25, jumped["next"] or "x", offset=0)
            print(
                f"100k overview: {elapsed:.3f}s, {baseline_queries} statements, "
                f"{len(json.dumps(overview, default=str))} bytes"
            )
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", record)


async def a_job_with_bound_steps(database: Database, keys: int) -> tuple[ledger.JobID, str]:
    """A job whose root expanded a ``run_step`` node into ``keys`` spawns, each bound to a result
    of the job's own; returns the job and the node's identity."""
    job = await a_job(database)
    root = await start_root(database, job)
    entities = PostgresEntityStorage(database)
    source = SourceArray(name="x", array=await entities.store(Array.scalar(1, int)))
    run = RunStep(
        name="s",
        step_name="s",
        inputs=frozendict({"x": source.content_id()}),
        map_over=("k",),
        join="exact",
        replicas=frozendict(),
        cache_key_prefix="root/s",
        type=ArrayType.of(("[k: int]", int)),
    )
    req = await a_request(database, 1)
    cache_keys: dict[Key, str] = {(k,): f"root/s[k:{k}]" for k in range(keys)}
    async with database.transaction() as tx:
        act = await execution.guard(tx, root.attempt_id)
        await entities.flush(tx)
        await ledger.record_emissions(
            tx, act, [Emission(source, ArrayType.of(int)), Emission(run, run.type)]
        )
        await ledger.record_step_spawns(tx, act, run.content_id(), cache_keys)
        await ledger.record_step_requests(tx, act, {key: req for key in cache_keys.values()}, "s")
        # Bulk seed the results, without running a step attempt per key.
        for statement in [
            """INSERT INTO step_results (id, request_cid, result_cid, job_attempt_id)
            SELECT md5(CAST(CAST(:job AS uuid) AS text) || cache_key)::uuid, request_cid,
                request_cid,
                (SELECT id FROM job_attempt_provenance WHERE job_id = CAST(:job AS uuid) LIMIT 1)
            FROM job_step_requests WHERE job_id = CAST(:job AS uuid)""",
            """INSERT INTO job_step_bindings (job_id, cache_key, step_result_id)
            SELECT job_id, cache_key, md5(CAST(job_id AS text) || cache_key)::uuid
            FROM job_step_requests WHERE job_id = CAST(:job AS uuid)""",
        ]:
            await tx.execute(text(statement), {"job": job})
    return job, run.content_id().cid_str()


def join_filter_rows(plan: dict[str, Any]) -> int:
    """The rows a plan's joins read and threw away, over all their loops: the work a join does
    when it matches by filter instead of by index."""
    removed = plan.get("Rows Removed by Join Filter", 0) * plan.get("Actual Loops", 1)
    return removed + sum(join_filter_rows(child) for child in plan.get("Plans", []))


# The bad plans come from statistics that predate the job, so the test makes them stale on purpose:
# the fourth job is missing from them. Forcing nested loops on fresh statistics stays fast.
@pytest.mark.timeout_seconds(60)
async def test_viewer_reads_stay_linear_for_a_job_missing_from_planner_statistics(
    database: Database,
):
    keys = 300
    async with database.transaction() as tx:
        for table in metadata.sorted_tables:
            await tx.execute(text(f"ALTER TABLE {table.name} SET (autovacuum_enabled = false)"))
    for _ in range(3):
        await a_job_with_bound_steps(database, keys)
    async with database.transaction() as tx:
        await tx.execute(text("ANALYZE"))
    job, node = await a_job_with_bound_steps(database, keys)
    statements: list[tuple[str, Any]] = []

    def record(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    async with a_client(database) as client:
        event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        try:
            overview = await level_overview(client, job, "root")
            page = await run_page(client, job, "root", node, {}, 50, None)
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    assert overview is not None
    (run,) = [n for n in overview["nodes"] if n["identity"] == node]
    assert run["counts"] == state_counts() | {"completed": keys}
    assert page["total"] == keys
    worst = 0
    async with database.transaction() as tx:
        for statement, parameters in statements:
            if not statement.lstrip().startswith(("SELECT", "WITH")):
                continue
            explained = await tx.connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters
            )
            worst = max(worst, join_filter_rows(explained.scalar_one()[0]["Plan"]))
    # Matching each key by index throws away only rows of the few-row tables (each spawn against
    # the four root attempts: about 4,000 here). Matching the job's rows by filter throws away about
    # keys²/2 (the version tokens' bindings count) to millions (the spawns' step states).
    assert worst < keys * keys // 4


async def test_dimension_pages_keep_values_and_cursor_when_other_selections_change(
    database: Database,
):
    async with a_client(database) as client:
        job = await a_job(database)
        root = await start_root(database, job)
        run, children = await expand_children(database, root)
        await start_invocation(database, job, children[1])
        node = run.content_id().cid
        async with database.transaction() as tx:
            node_row = (await tx.execute(select(nodes).where(nodes.c.identity == node))).one()
            await tx.execute(
                nodes.update()
                .where(nodes.c.identity == node)
                .values(
                    params={**node_row.params, "map_over": ["k", "context"]},
                    type={"dims": [["k", "int"], ["context", "str"]], "schema": "int"},
                )
            )
            for child, key in zip(children, [[0, "x"], [1, "y"]], strict=True):
                await tx.execute(
                    spawns.update().where(spawns.c.child_invocation_id == child.id).values(key=key)
                )
            third = uuid.uuid4()
            await tx.execute(
                workflow_invocations.insert().values(
                    id=third,
                    job_id=job,
                    pathname="third",
                    workflow_name="child_wf",
                    cache_key_prefix="third",
                )
            )
            await tx.execute(
                spawns.insert().values(
                    invocation_id=root.invocation.id,
                    node_identity=node,
                    key=[0, "y"],
                    child_invocation_id=third,
                )
            )

        async def keys(dim="k", selection=None, search="", first=25, after=None):
            return await key_page(
                client, job, "root", cid_to_str(node), dim, selection or {}, search, first, after
            )

        assert (await keys())["items"] == [
            {"value": 0, "compatible": True, "counts": state_counts() | {"queued": 2}},
            {"value": 1, "compatible": True, "counts": state_counts() | {"running": 1}},
        ]
        narrowed = await keys(selection={"context": "x"})
        assert narrowed["total"] == 2
        assert narrowed["items"] == [
            {"value": 0, "compatible": True, "counts": state_counts() | {"queued": 1}},
            {"value": 1, "compatible": False, "counts": state_counts() | {"running": 1}},
        ]
        first = await keys(selection={"context": "x"}, first=1)
        next_page = await keys(selection={"context": "y"}, first=1, after=first["next"])
        assert next_page["items"] == [
            {"value": 1, "compatible": True, "counts": state_counts() | {"running": 1}}
        ]
        assert next_page["total"] == 2 and next_page["next"] is None
        assert (await keys(selection={"k": 999}))["items"] == (await keys())["items"]
        assert all(
            not item["compatible"]
            for item in (await keys(dim="context", selection={"k": "0"}))["items"]
        )
        assert (await keys(dim="context", selection={"k": 1}, search="x"))["items"] == [
            {"value": "x", "compatible": False, "counts": state_counts() | {"queued": 1}},
        ]


async def test_current_output_batches_skip_empty_arrays_and_share_cached_cids(database: Database):
    from fxtr.runner.postgres.debug_web.output import OutputStore

    async with a_client(database) as client:
        job = await a_job(database)
        root = await start_root(database, job)
        run, _ = await expand_children(database, root)
        empty = await client.entities.store(Array.from_items([], ("[inner: int]", int)))
        value = await client.entities.store(Array.from_items([((0,), 42)], ("[inner: int]", int)))
        async with database.transaction() as tx:
            await client.entities.flush(tx)
            await tx.execute(
                nodes.update()
                .where(
                    nodes.c.invocation_id == root.invocation.id,
                    nodes.c.identity == run.content_id().cid,
                )
                .values(type={"dims": [["k", "int"], ["inner", "int"]], "schema": "int"})
            )
            await tx.execute(
                text("""
                INSERT INTO workflow_invocations
                    (id, job_id, pathname, workflow_name, cache_key_prefix, result_cid,
                     job_attempt_id)
                SELECT md5(:salt || i::text)::uuid, :job, 'child-' || i, 'child_wf', 'child-' || i,
                       CASE WHEN i < 40 THEN CAST(:empty AS bytea) ELSE CAST(:value AS bytea) END,
                       (SELECT id FROM job_attempt_provenance WHERE job_id = :job LIMIT 1)
                FROM generate_series(2, 79) i
            """),
                {
                    "salt": str(uuid.uuid4()),
                    "job": job,
                    "empty": empty.entity_id.cid,
                    "value": value.entity_id.cid,
                },
            )
            await tx.execute(
                text("""
                INSERT INTO spawns(invocation_id, node_identity, key, child_invocation_id)
                SELECT :root, :node, jsonb_build_array(substring(pathname from 7)::int), id
                FROM workflow_invocations WHERE job_id=:job AND pathname LIKE 'child-%'
            """),
                {"root": root.invocation.id, "node": run.content_id().cid, "job": job},
            )
            await tx.execute(
                workflow_invocations.update()
                .where(
                    workflow_invocations.c.job_id == job,
                    workflow_invocations.c.pathname.like("root/child%"),
                )
                .values(result_cid=empty.entity_id.cid, job_attempt_id=producer(job))
            )
        source: OutputSource = {
            "jobId": str(job),
            "pathname": "root",
            "nodeId": run.content_id().cid_str(),
        }
        store = OutputStore(client)
        statements: list[str] = []

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        try:
            first = await store.page(source)
            assert [row["key"] for row in first["rows"]] == [[i, 0] for i in range(40, 65)]
            assert first["total"] is None and first["hasMore"]
            assert len(store.arrays) == 2
            assert sum("entities.data" in statement for statement in statements) == 2
            statements.clear()
            last = await store.page(source, offset=25)
            assert [row["key"] for row in last["rows"]] == [[i, 0] for i in range(65, 80)]
            assert last["total"] == 40 and not last["hasMore"]
            assert not any("entities.data" in statement for statement in statements)
            for kwargs in [{"last": True}, {"offset": 10_000}]:
                jumped = await store.page(source, **kwargs)
                assert jumped["offset"] == 25 and jumped["total"] == 40
                assert [row["key"] for row in jumped["rows"]] == [[i, 0] for i in range(65, 80)]
            match = await store.page(source, filters={"key:k": "79"})
            assert match["total"] == 1 and match["rows"][0]["key"] == [79, 0]
            sorted_page = await store.page(source, sort={"key": "key:k", "direction": "desc"})
            assert sorted_page["total"] == 40 and sorted_page["rows"][0]["key"] == [79, 0]

            async def disconnected():
                return True

            with pytest.raises(ValueError, match="cancelled"):
                await store.page(source, disconnected=disconnected)
        finally:
            store.close()
            event.remove(database.engine.sync_engine, "before_cursor_execute", record)


async def test_invocation_pages_carry_results_and_filter_and_sort_on_every_cell(database: Database):
    from fxtr.core import codec
    from fxtr.runner.postgres.debug_web.output import OutputStore

    async with a_client(database) as client:
        job = await a_job(database)
        root = await start_root(database, job)
        run, children = await expand_children(database, root)
        # k=0 has no result; k=1 produced 50; k=2..9 produced 10 - k.
        results = {k: await client.entities.store(Array.scalar(10 - k, int)) for k in range(2, 10)}
        fifty = await client.entities.store(Array.scalar(50, int))
        async with database.transaction() as tx:
            await client.entities.flush(tx)
            await tx.execute(
                workflow_invocations.update()
                .where(workflow_invocations.c.id == children[1].id)
                .values(result_cid=fifty.entity_id.cid, job_attempt_id=producer(job))
            )
            for k, result in results.items():
                await tx.execute(
                    text("""
                    INSERT INTO workflow_invocations
                        (id, job_id, pathname, workflow_name, cache_key_prefix, result_cid,
                         job_attempt_id)
                    VALUES (:id, :job, :pathname, 'child_wf', :pathname, :result,
                            (SELECT id FROM job_attempt_provenance WHERE job_id = :job LIMIT 1))
                """),
                    {
                        "id": uuid.uuid4(),
                        "job": job,
                        "pathname": f"root/child[k:{k}]",
                        "result": result.entity_id.cid,
                    },
                )
                await tx.execute(
                    text("""
                    INSERT INTO spawns(invocation_id, node_identity, key, child_invocation_id)
                    SELECT :root, :node, jsonb_build_array(CAST(:k AS int)), id
                    FROM workflow_invocations WHERE job_id=:job AND pathname=:pathname
                """),
                    {
                        "root": root.invocation.id,
                        "node": run.content_id().cid,
                        "k": k,
                        "job": job,
                        "pathname": f"root/child[k:{k}]",
                    },
                )
        store = OutputStore(client)
        node = run.content_id().cid_str()

        async def page(**kwargs):
            return await store.invocation_page(job, "root", node, {}, **kwargs)

        def keys(found) -> list:
            return [item["spawn"]["key"][0] for item in found["items"]]

        try:
            plain = await page(first=5)
            assert plain["total"] == 10 and plain["offset"] == 0 and keys(plain) == [0, 1, 2, 3, 4]
            # Each invocation produces one row: its value comes with it; none without a result.
            assert plain["items"][0]["rowCount"] is None and plain["items"][0]["value"] is None
            value = plain["items"][1]["value"]
            assert plain["items"][1]["rowCount"] == 1 and value is not None
            assert codec.decode(base64.b64decode(value)) == 50
            assert (await page(first=4, last=True))["offset"] == 8
            assert keys(await page(first=4, offset=6)) == [4, 5, 6, 7]

            matched = await page(filters={"value:": "50"})
            assert matched["total"] == 1 and keys(matched) == [1]
            # No result, no row count: a filter on it leaves k=0 out.
            assert (await page(filters={"rows:": "1"}))["total"] == 9
            assert keys(await page(filters={"key:k": "7"})) == [7]
            state = plain["items"][0]["spawn"]["state"]
            assert 0 in keys(await page(filters={"state:": state}))

            descending = await page(sort={"key": "value:", "direction": "desc"})
            assert keys(descending) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 0]
            ascending = await page(sort={"key": "value:", "direction": "asc"}, first=5, offset=5)
            # The invocation with no value sorts last either way.
            assert keys(ascending) == [4, 3, 2, 1, 0] and ascending["total"] == 10
            assert keys(await page(sort={"key": "key:k", "direction": "desc"}, first=3)) == [
                9,
                8,
                7,
            ]

            # Several rows each: the row count, but no value to filter or show.
            async with database.transaction() as tx:
                await tx.execute(
                    nodes.update()
                    .where(
                        nodes.c.invocation_id == root.invocation.id,
                        nodes.c.identity == run.content_id().cid,
                    )
                    .values(type={"dims": [["k", "int"], ["inner", "int"]], "schema": "int"})
                )
            several = await page(first=2)
            assert [item["value"] for item in several["items"]] == [None, None]
            assert several["items"][1]["rowCount"] == 1
            assert (await page(filters={"value:": "50"}))["total"] == 0

            with pytest.raises(ValueError, match="invalid sort"):
                await page(sort={"key": "value:", "direction": "up"})  # type: ignore[typeddict-item]
            with pytest.raises(ValueError, match="invalid dimension selection"):
                await store.invocation_page(job, "root", node, {"other": 1})
        finally:
            store.close()


async def test_output_selection_filters_inner_dims_and_lists_their_values(
    database: Database, monkeypatch: pytest.MonkeyPatch
):
    from fxtr.runner.postgres.debug_web.output import OutputStore

    async with a_client(database) as client:
        job = await a_job(database)
        root = await start_root(database, job)
        run, children = await expand_children(database, root)
        results = [
            await client.entities.store(
                Array.from_items([((0,), "a"), ((1,), "b")], ("[inner: int]", str))
            ),
            await client.entities.store(
                Array.from_items([((1,), "c"), ((2,), "d")], ("[inner: int]", str))
            ),
        ]
        async with database.transaction() as tx:
            await client.entities.flush(tx)
            await tx.execute(
                nodes.update()
                .where(
                    nodes.c.invocation_id == root.invocation.id,
                    nodes.c.identity == run.content_id().cid,
                )
                .values(type={"dims": [["k", "int"], ["inner", "int"]], "schema": "str"})
            )
            for child, result in zip(children, results, strict=True):
                await tx.execute(
                    workflow_invocations.update()
                    .where(workflow_invocations.c.id == child.id)
                    .values(result_cid=result.entity_id.cid, job_attempt_id=producer(job))
                )
        run_source: OutputSource = {
            "jobId": str(job),
            "pathname": "root",
            "nodeId": run.content_id().cid_str(),
        }
        store = OutputStore(client)
        try:

            async def keys(source: OutputSource, selection: dict) -> list:
                page = await store.page({**source, "selection": selection})
                return [row["key"] for row in page["rows"]]

            assert await keys(run_source, {"inner": 1}) == [[0, 1], [1, 1]]
            assert await keys(run_source, {"k": 1, "inner": 1}) == [[1, 1]]
            assert await keys(run_source, {"inner": "1"}) == []
            with pytest.raises(ValueError, match="invalid dimension selection"):
                await store.page({**run_source, "selection": {"other": 1}})

            array_source: OutputSource = {"cid": results[1].entity_id.cid_str()}
            assert await keys(array_source, {}) == [[1], [2]]
            assert await keys(array_source, {"inner": 2}) == [[2]]
            with pytest.raises(ValueError, match="invalid dimension selection"):
                await store.page({**array_source, "selection": {"k": 0}})

            values = await store.dim_values({**run_source, "selection": {"k": 0}}, "inner")
            assert values == {
                "items": [
                    {"value": 0, "compatible": True},
                    {"value": 1, "compatible": True},
                    {"value": 2, "compatible": False},
                ],
                "total": 3,
                "offset": 0,
            }
            # The dim's own pick does not narrow its values.
            assert (await store.dim_values({**run_source, "selection": {"inner": 2}}, "inner"))[
                "total"
            ] == 3
            assert (await store.dim_values(run_source, "k"))["items"] == [
                {"value": 0, "compatible": True},
                {"value": 1, "compatible": True},
            ]
            paged = await store.dim_values(run_source, "inner", first=2, offset=5)
            assert paged["offset"] == 2 and paged["items"] == [{"value": 2, "compatible": True}]
            assert (await store.dim_values(array_source, "inner", search="2"))["items"] == [
                {"value": 2, "compatible": True}
            ]
            with pytest.raises(ValueError, match="unknown dimension"):
                await store.dim_values(run_source, "other")

            # Summarized arrays are not read again: searching and paging decode nothing.
            async def no_load(cids: list[str]):
                raise AssertionError(f"reloaded {cids}")

            store.arrays.clear()
            monkeypatch.setattr(store, "load", no_load)
            searched = await store.dim_values({**run_source, "selection": {"k": 0}}, "inner", "2")
            assert searched["items"] == [{"value": 2, "compatible": False}]
            assert (await store.dim_values(run_source, "inner", first=2))["items"] == [
                {"value": 0, "compatible": True},
                {"value": 1, "compatible": True},
            ]
        finally:
            store.close()
