"""The debug viewer's GraphQL backend against a real, completed job.

Runs a tiny workflow (a step behind one run_step node) end to end in a job runner, then exercises
the resolvers through :func:`ariadne.graphql` and checks the raw-CBOR array/entity plumbing. The
whole server is exercised in-process against the test database; nothing here writes through it.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import random
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import anyio
import pytest
from postgres_testing import FakeEnvironment, a_client

from fxtr.core import codec
from fxtr.core.entity_data import EntityID, cid_from_str
from fxtr.experiment.registry import Function, FunctionRegistry
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.project.code_version import CodeVersion, RepoSource
from fxtr.project.config import Project, ViewsConfig
from fxtr.project.views import (
    VIEW_API_VERSION,
    VIEW_BUNDLE_MANIFEST_FILENAME,
    VIEW_BUNDLE_SOURCES_FILENAME,
    bundle_version,
    sources_fingerprint,
)

pytest.importorskip("sqlalchemy")
pytest.importorskip("ariadne")

from ariadne import graphql  # noqa: E402
from asgi_testing import PAGE_ORIGIN, a_page_socket  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Mount  # noqa: E402
from starlette.types import ASGIApp, Message  # noqa: E402

from fxtr.entity_defns.array import ARRAY_TYPE, Array  # noqa: E402
from fxtr.experiment.handles import ArrayHandle  # noqa: E402
from fxtr.runner.postgres.client import LocalProjectClient  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.debug_web import app as app_module  # noqa: E402
from fxtr.runner.postgres.debug_web import ports as ports_module  # noqa: E402
from fxtr.runner.postgres.debug_web import server as server_module  # noqa: E402
from fxtr.runner.postgres.debug_web.app import (  # noqa: E402
    MAX_ENTITIES_PER_REQUEST,
    VIEWS_VERSION_HEADER,
    Viewer,
    ViewerFiles,
    build_schema,
    checkout_frontend,
    create_app,
    default_frontend_dist,
    docstring_at,
    enclosing_repository,
    experiment_readme_response,
    file_request_path,
    source_response,
    views_response,
)
from fxtr.runner.postgres.debug_web.contracts import OutputSource, state_counts  # noqa: E402
from fxtr.runner.postgres.debug_web.dev_server import (  # noqa: E402
    DEV_SERVER_PORT_OFFSET,
    DevServerConfig,
    dev_server_port,
    free_port,
    run_dev_server,
)
from fxtr.runner.postgres.debug_web.ports import ViewerPortError, viewer_url  # noqa: E402
from fxtr.runner.postgres.debug_web.server import serve  # noqa: E402
from fxtr.runner.postgres.debug_web.viewer_build import (  # noqa: E402
    VIEWER_SOURCES_FILENAME,
    ViewerBuildError,
    build_viewer,
    refresh_viewer,
    viewer_sources_fingerprint,
    viewer_stale_reason,
)
from fxtr.runner.postgres.debug_web.views_watch import ViewsWatcher  # noqa: E402
from fxtr.runner.postgres.ledger import JobID  # noqa: E402
from fxtr.runner.postgres.runner import RunnerSettings  # noqa: E402

pytestmark = pytest.mark.anyio


@step(inputs={"seed": str}, output=int)
async def roll(context: StepContext, seed: str) -> int:
    context.log(f"rolling {seed}")
    return 42


@workflow(inputs={"seed": str}, output=int)
async def roll_once(context: WorkflowContext, seed: ArrayHandle[str]) -> ArrayHandle[int]:
    context.log("running roll_once")
    return context.run_step("roll", roll, {"seed": seed})


_FAST = RunnerSettings(heartbeat_interval=0.5, reconcile_interval=1.0, poll_interval=0.05)


@asynccontextmanager
async def _completed_job(database: Database) -> AsyncIterator[tuple[LocalProjectClient, JobID]]:
    """Run ``roll_once`` to completion through a local client and yield the client, still
    entered, with the finished job's id."""
    async with a_client(database, roll, roll_once, settings=_FAST) as client:
        with anyio.fail_after(15):
            status = await client.run(roll_once, {"seed": Array.scalar("s", str)})
        assert status.state == "succeeded", status
        yield client, status.job_id


async def _run(schema: Any, client: LocalProjectClient, query: str, **variables: Any) -> dict:
    ok, result = await graphql(
        schema, {"query": query, "variables": variables}, context_value={"client": client}
    )
    assert ok, result
    assert "errors" not in result, result["errors"]
    return result["data"]


async def test_job_view(database: Database) -> None:
    schema = build_schema()
    async with _completed_job(database) as (client, job):
        from fxtr.runner.postgres.debug_web.browse import job_page, level_overview, run_page

        overview = await level_overview(client, job, "root")
        assert overview is not None
        assert overview["state"] == "completed"
        assert "spawns" not in overview and "attempts" not in overview
        assert overview["attemptCount"] == 1
        assert overview["finishedAt"] is not None
        assert (await job_page(client, 10, None))["items"][0]["state"] == "succeeded"
        run_id = next(node["identity"] for node in overview["nodes"] if node["counts"])
        data = await _run(
            schema,
            client,
            """
            query($job: ID!, $cids: [String!]!) {
              jobSummaries(first: 1) { items { id workflowName root createdAt state failed } next }
              jobSummary(id: $job) { id state failed }
              entityMetas(cids: $cids) { cid typeTag }
              levelOverview(jobId: $job, pathname: "root") {
                pathname workflowName state resultCid startedAt finishedAt
                error { typeName message traceback }
                version attemptCount codeVersion returnedNode awaitingNode asOf
                nodes { identity kind name type params inputs { label identity }
                  valueCid mapOver replicaDims version
                  counts { queued running completed failed interrupted conflict stopped }
                  activity { firstStartedAt lastFinishedAt finished oldestRunningSince }
                  failures { typeName message count }
                }
              }
            }
        """,
            job=str(job),
            cids=[overview["resultCid"]],
        )

        def iso(value):
            return value.isoformat() if value is not None else None

        def node_json(node):
            activity = node["activity"]
            if activity is None:
                return node
            times = ("firstStartedAt", "lastFinishedAt", "oldestRunningSince")
            return node | {"activity": activity | {key: iso(activity[key]) for key in times}}

        run_activity = next(node["activity"] for node in overview["nodes"] if node["counts"])
        assert run_activity is not None
        assert run_activity["finished"] == 1 and run_activity["oldestRunningSince"] is None
        assert {**data["levelOverview"], "asOf": None} == overview | {
            "startedAt": iso(overview["startedAt"]),
            "finishedAt": iso(overview["finishedAt"]),
            "asOf": None,
            "nodes": [node_json(node) for node in overview["nodes"]],
        }
        assert data["jobSummary"]["id"] == str(job)
        assert data["jobSummaries"]["items"][0]["id"] == str(job)
        assert data["entityMetas"][0]["typeTag"] == ARRAY_TYPE
        page = await run_page(client, job, "root", run_id, {}, 1, None)
        assert page["total"] == 1
        assert page["items"][0]["state"] == "completed"
        from fxtr.runner.postgres.debug_web.output import OutputStore

        outputs = OutputStore(client)
        try:
            output = await outputs.page({"jobId": str(job), "pathname": "root", "nodeId": run_id})
            assert output["total"] == 1 and not output["hasMore"]
            assert codec.decode(base64.b64decode(output["rows"][0]["value"])) == 42
            ok, response = await graphql(
                schema,
                {
                    "query": """query($source: OutputSource!) { outputPage(source: $source) {
                        type rows { key value } total offset hasMore
                    } }""",
                    "variables": {"source": {"cid": overview["resultCid"]}},
                },
                context_value={"client": client, "outputs": outputs},
            )
            assert ok and "errors" not in response, response
            assert response["data"]["outputPage"]["rows"] == output["rows"]
        finally:
            outputs.close()
        # The job overview carries shallow invocation/step lists (no nodes/attempts).
        result = (
            await _run(
                schema,
                client,
                """
                query($id: ID!) {
                  job(id: $id) {
                    id state failed resultCid
                    jobAttempts { id seq state }
                    invocations { pathname workflowName state }
                    steps { cacheKey stepName state }
                  }
                }
                """,
                id=str(job),
            )
        )["job"]
        assert result["id"] == str(job)
        assert result["state"] == "succeeded"
        assert result["failed"] is False
        assert result["resultCid"] is not None
        (root_summary,) = [i for i in result["invocations"] if i["pathname"] == "root"]
        assert root_summary["workflowName"] == roll_once.name
        assert root_summary["state"] == "completed"
        (step_summary,) = result["steps"]
        assert step_summary["stepName"] == roll.name
        assert step_summary["state"] == "completed"

        # The full invocation is fetched per-entity; ids are the composite cache keys.
        inv = (
            await _run(
                schema,
                client,
                """
                query($j: ID!, $p: String!) {
                  invocation(jobId: $j, pathname: $p) {
                    id pathname workflowName state
                    nodes { id identity kind name valueCid }
                    spawns { stepCacheKey childPathname resultCid state }
                    attempts { id state logs { message } }
                  }
                }
                """,
                j=str(job),
                p="root",
            )
        )["invocation"]
        assert inv["id"] == f"{job}/root"
        assert any(n["kind"] == "run_step" for n in inv["nodes"])
        assert all(n["id"].startswith(f"{job}/root/") for n in inv["nodes"])
        cache_keys = [s["stepCacheKey"] for s in inv["spawns"] if s["stepCacheKey"]]
        assert cache_keys, inv["spawns"]
        # The run-step spawn's result is the bound step's result.
        assert all(s["resultCid"] is not None for s in inv["spawns"] if s["stepCacheKey"])
        assert all(s["state"] == "completed" for s in inv["spawns"])
        assert any(
            "running roll_once" in log["message"] for a in inv["attempts"] for log in a["logs"]
        )

        # The full step, fetched per-entity, is bound and carries the step's ctx.log line.
        pin = (
            await _run(
                schema,
                client,
                """
                query($j: ID!, $k: String!) {
                  step(jobId: $j, cacheKey: $k) {
                    id cacheKey stepName state resultCid
                    attempt { id state logs { message } }
                  }
                }
                """,
                j=str(job),
                k=cache_keys[0],
            )
        )["step"]
        assert pin["id"] == f"{job}/{cache_keys[0]}"
        assert pin["cacheKey"] == cache_keys[0]
        assert pin["stepName"] == roll.name
        assert pin["state"] == "completed"
        assert pin["resultCid"] is not None
        assert any("rolling s" in log["message"] for log in pin["attempt"]["logs"])

        # Missing per-entity targets resolve to null (nullable fields, not errors).
        missing_inv = await _run(
            schema,
            client,
            'query($j: ID!){ invocation(jobId: $j, pathname: "nope") { id } }',
            j=str(job),
        )
        assert missing_inv["invocation"] is None
        missing_step = await _run(
            schema, client, 'query($j: ID!){ step(jobId: $j, cacheKey: "nope") { id } }', j=str(job)
        )
        assert missing_step["step"] is None


async def test_jobs_and_step_cache(database: Database) -> None:
    schema = build_schema()
    async with _completed_job(database) as (client, job):
        jobs = (await _run(schema, client, "{ jobs { id state } }"))["jobs"]
        assert [j["id"] for j in jobs] == [str(job)]
        assert jobs[0]["state"] == "succeeded"

        cache = (
            await _run(
                schema, client, "{ stepCache { cacheKey stepName state attempt { state } } }"
            )
        )["stepCache"]
        (entry,) = cache
        assert entry["stepName"] == roll.name
        assert entry["state"] == "completed"
        assert entry["attempt"]["state"] == "completed"


async def test_output_array_cache_eviction_and_default_pages(database: Database, monkeypatch):
    from fxtr.runner.postgres.debug_web import output

    async with a_client(database) as client:
        arrays = [
            Array.from_items([((i,), i + j) for i in range(60)], ("[i: int]", int))
            for j in range(3)
        ]
        sources: list[OutputSource] = [
            {"cid": (await client.entities.store(array)).entity_id.cid_str()} for array in arrays
        ]
        async with database.transaction() as tx:
            await client.entities.flush(tx)
        monkeypatch.setattr(output, "MAX_ARRAYS", 2)
        store = output.OutputStore(client)
        try:
            first = await store.page(sources[0])
            assert len(first["rows"]) == 25 and first["total"] == 60 and first["hasMore"]
            assert (await store.page(sources[0], offset=25))["rows"][0]["key"] == [25]
            last = await store.page(sources[0], offset=50)
            assert len(last["rows"]) == 10 and not last["hasMore"]
            await store.page(sources[1])
            await store.page(sources[2])
            assert len(store.arrays) == 2 and sources[0].get("cid") not in store.arrays
            assert await store.page(sources[0]) == first
            assert len(store.arrays) == 2
            with pytest.raises(ValueError, match="offset"):
                await store.page(sources[0], offset=-1)
            with pytest.raises(ValueError):
                await store.page(sources[0], first=201)
            for invalid in [
                {},
                {"cid": sources[0].get("cid"), "nodeId": "node"},
                {"jobId": "job"},
            ]:
                with pytest.raises(ValueError, match="source"):
                    await store.page(invalid)
            # A single array larger than the cache budget still works without retaining it.
            store.close()
            monkeypatch.setattr(output, "MAX_BYTES", 1)
            assert await store.page(sources[0]) == first
            assert not store.arrays and store.bytes == 0
        finally:
            store.close()


ARRAY_QUERY = "query($c: String!){ array(cid: $c){ cid type rowCount rows { key value } } }"
ARRAY_LIMIT_QUERY = (
    "query($c: String!, $n: Int){ array(cid: $c, limit: $n){ rowCount rows { key value } } }"
)
ENTITY_QUERY = "query($c: String!){ entity(cid: $c){ cid typeTag } }"


async def test_array_and_entity_views(database: Database) -> None:
    schema = build_schema()
    async with _completed_job(database) as (client, job):
        query = "query($id: ID!){ job(id: $id) { resultCid } }"
        result_cid = (await _run(schema, client, query, id=str(job)))["job"]["resultCid"]

        view = (await _run(schema, client, ARRAY_QUERY, c=result_cid))["array"]
        assert view["cid"] == result_cid
        assert view["rowCount"] == 1
        (row,) = view["rows"]
        assert row["key"] == []
        # The cell is raw DRISL the frontend decodes; here it decodes to the scalar 42.
        assert codec.decode(base64.b64decode(row["value"])) == 42

        # `limit` caps the rows returned (for previews) while `rowCount` stays the total.
        limited = (await _run(schema, client, ARRAY_LIMIT_QUERY, c=result_cid, n=0))["array"]
        assert limited["rowCount"] == 1
        assert limited["rows"] == []

        meta = (await _run(schema, client, ENTITY_QUERY, c=result_cid))["entity"]
        assert meta["cid"] == result_cid
        assert meta["typeTag"] == ARRAY_TYPE  # the stored Array entity's type id

        # The raw bytes the /entity route would serve are the stored Array entity itself (its
        # dims, rows, schema, and $type) — what the frontend's DAG-CBOR viewer would render.
        raw = await client.entities.load_raw_bytes(EntityID(cid=cid_from_str(result_cid)))
        assert raw is not None
        decoded = codec.decode(raw[0])
        assert isinstance(decoded, dict) and decoded["rows"] == [[42]]

        missing = await _run(schema, client, ENTITY_QUERY, c="b" + "a" * 50)
        assert missing["entity"] is None


async def test_many_entities_come_back_in_one_response(database: Database) -> None:
    async with _completed_job(database) as (client, job):
        app = create_app(client)
        query = "query($id: ID!){ job(id: $id) { resultCid } }"
        result_cid = (await _run(build_schema(), client, query, id=str(job)))["job"]["resultCid"]
        stored = await client.entities.load_raw_bytes(EntityID(cid=cid_from_str(result_cid)))
        assert stored is not None
        missing = EntityID(cid=codec.compute_cid(codec.encode({"$type": "t", "n": 0})))

        def post(payload: object) -> Any:
            body = json.dumps(payload).encode()
            return _request(app, "/api/entities", method="POST", body=body)

        cids = [result_cid, missing.cid_str(), result_cid]
        status, headers, body = await post({"cids": cids})
        assert (status, headers["content-type"]) == (200, "application/cbor")
        # In the order asked, each the bytes GET /api/entity/{cid} serves, null for the missing.
        assert codec.decode(body) == [stored[0], None, stored[0]]

        assert codec.decode((await post({"cids": []}))[2]) == []
        for bad in [{"cids": ["not a cid"]}, {"cids": "x"}, [result_cid], {"cids": [1]}]:
            assert (await post(bad))[0] == 400
        too_many, _, reason = await post({"cids": [result_cid] * (MAX_ENTITIES_PER_REQUEST + 1)})
        assert too_many == 400 and b"at most" in reason


async def test_app_construction_and_graphql_over_http_only() -> None:
    async with a_client(Database("postgresql+asyncpg://unused", pg_schema="unused")) as client:
        app = create_app(client)
        (api,) = [r for r in app.routes if getattr(r, "path", None) == "/api"]
        paths = {getattr(r, "path", None) for r in api.routes}  # type: ignore[attr-defined]
        assert "/entity/{cid}" in paths and "/views/{name}" in paths
        # Nothing reads GraphQL over a WebSocket, so no page opens one: the viewer's or another's.
        for origin in (PAGE_ORIGIN, "https://example.com", None):
            async with a_page_socket(app, "/api/graphql/", origin=origin) as page:
                assert not page.accepted


# ---- live state: spawn states and invocation versions over a job in progress ----

GATE: anyio.Event | None = None
"""Holds every ``gated`` call whose seed is ``"slow"`` until set."""


@step(inputs={"seed": str}, output=int)
async def gated(context: StepContext, seed: str) -> int:
    if seed == "slow":
        assert GATE is not None
        await GATE.wait()
    return len(seed)


@workflow(inputs={"seed": str}, output=int)
async def gated_inner(context: WorkflowContext, seed: ArrayHandle[str]) -> ArrayHandle[int]:
    return context.run_step("gated", gated, {"seed": seed})


@workflow(inputs={"seeds": ("[seed: str]", str)}, output=("[seed: str]", int))
async def gated_sweep(context: WorkflowContext, seeds: ArrayHandle[str]) -> ArrayHandle[int]:
    context.run_child_workflow("inner", gated_inner, {"seed": seeds}, map_over=["seed"])
    return context.run_step("gated", gated, {"seed": seeds}, map_over=["seed"])


SEEDS = Array.from_items([(("fast",), "fast"), (("slow",), "slow")], ("[seed: str]", str))

LIVE_QUERY = """
query($j: ID!, $p: String!) {
  invocationVersion(jobId: $j, pathname: $p)
  invocation(jobId: $j, pathname: $p) {
    state
    spawns { key childPathname stepCacheKey state resultCid }
  }
}
"""


async def _live(schema: Any, client: LocalProjectClient, job: JobID) -> tuple[str, dict[str, Any]]:
    """The root's version and a digest of its state: its own and each spawn's, by kind and seed."""
    data = await _run(schema, client, LIVE_QUERY, j=str(job), p="root")
    inv = data["invocation"]
    spawns = {
        ("child" if s["childPathname"] else "step", s["key"][0]): s["state"] for s in inv["spawns"]
    }
    return data["invocationVersion"], {"state": inv["state"], "spawns": spawns}


async def _await_live(
    schema: Any, client: LocalProjectClient, job: JobID, expected: dict[str, Any]
) -> str:
    """Poll until the root's digest is ``expected``; return the version it had then."""
    with anyio.fail_after(15):
        while True:
            version, digest = await _live(schema, client, job)
            if digest == expected:
                return version
            await anyio.sleep(0.02)


async def test_spawn_states_and_versions_follow_a_live_job(database: Database) -> None:
    global GATE
    GATE = anyio.Event()
    schema = build_schema()
    async with a_client(database, gated, gated_inner, gated_sweep, settings=_FAST) as client:
        missing = await _run(
            schema,
            client,
            'query($j: ID!){ invocationVersion(jobId: $j, pathname: "nope") }',
            j="00000000-0000-0000-0000-000000000000",
        )
        assert missing["invocationVersion"] is None

        # Created but not yet taken, so it is observably stopped before it runs.
        job = await client.jobs.create(gated_sweep, {"seeds": SEEDS})
        untaken, digest = await _live(schema, client, job)
        assert digest == {"state": "stopped", "spawns": {}}
        assert (await _live(schema, client, job))[0] == untaken, (
            "unchanged state, unchanged version"
        )

        with anyio.fail_after(30):
            handle = await client.resume(job)
            # Both runs expanded; the fast calls finished, the slow ones wait on the gate.
            blocked = await _await_live(
                schema,
                client,
                job,
                {
                    "state": "running",
                    "spawns": {
                        ("step", "fast"): "completed",
                        ("step", "slow"): "running",
                        ("child", "fast"): "completed",
                        ("child", "slow"): "running",
                    },
                },
            )
            assert blocked != untaken
            from fxtr.runner.postgres.debug_web.browse import key_page, level_overview, run_page

            overview = await level_overview(client, job, "root")
            assert overview is not None and overview["state"] == "running"
            for node in overview["nodes"]:
                if node["counts"] is None:
                    continue
                node_id = node["identity"]
                assert node["counts"] == state_counts() | {"completed": 1, "running": 1}
                page = await key_page(client, job, "root", node_id, "seed", {}, "", 1, None)
                assert page["total"] == 2 and page["items"][0]["value"] == "fast"
                next_page = await key_page(
                    client, job, "root", node_id, "seed", {}, "", 1, page["next"]
                )
                assert next_page["items"][0]["value"] == "slow"
                pages = await _run(
                    schema,
                    client,
                    """
                    query($job: ID!, $node: String!) {
                      runPage(jobId: $job, pathname: "root", nodeId: $node,
                        selection: {seed: "slow"}) {
                        items { nodeIdentity key childPathname stepCacheKey resultCid state }
                        total next
                      }
                      keyPage(jobId: $job, pathname: "root", nodeId: $node,
                        dim: "seed", selection: {}) {
                        items { value compatible
                          counts { queued running completed failed interrupted conflict stopped } }
                        total offset next
                      }
                    }
                """,
                    job=str(job),
                    node=node_id,
                )
                assert pages["keyPage"]["total"] == 2
                assert pages["runPage"]["items"][0]["state"] == "running"
                picked = await run_page(client, job, "root", node_id, {"seed": "slow"}, 10, None)
                assert picked["total"] == 1 and picked["items"][0]["state"] == "running"
                unfinished = await run_page(
                    client, job, "root", node_id, {}, 10, None, unfinished=True
                )
                assert [item["key"] for item in unfinished["items"]] == [["slow"]]
                assert unfinished["total"] == 1
            from fxtr.runner.postgres.debug_web.output import OutputStore

            outputs = OutputStore(client)
            source: OutputSource = {
                "jobId": str(job),
                "pathname": "root",
                "nodeId": next(node["identity"] for node in overview["nodes"] if node["counts"]),
            }
            before = await outputs.page(source)
            assert before["total"] == 1
            GATE.set()
            status = await handle.wait()
            assert status.state == "succeeded", status
            finished, digest = await _live(schema, client, job)
            assert digest["state"] == "completed"
            assert set(digest["spawns"].values()) == {"completed"}
            assert finished not in (untaken, blocked)
            try:
                current = await outputs.page(source)
                assert current["total"] == 2 and len(current["rows"]) == 2
                assert current["rows"][0] == before["rows"][0]
            finally:
                outputs.close()


# ---- provenance: which code produced each attempt and result ----

PROVENANCE_QUERY = """
query($j: ID!, $key: String!) {
  invocation(jobId: $j, pathname: "root") {
    provenance { jobId commit }
    attempts { state provenance { jobId jobAttemptId commit } }
  }
  step(jobId: $j, cacheKey: $key) {
    provenance { jobId jobAttemptId commit tree ephemeral projectPath environmentVerified }
    attempt { provenance { jobId commit } }
  }
}
"""


async def test_provenance_names_the_process_that_produced_each_result(database: Database) -> None:
    """A second job at another commit reuses the first job's step from the cache: its own root
    says the second commit, and its step says the first job and commit, which produced it."""
    schema = build_schema()
    first = FakeEnvironment(commit="a" * 40, tree="b" * 40)
    second = FakeEnvironment(commit="c" * 40, tree="d" * 40, ephemeral=True)
    jobs: list[JobID] = []
    for environment in (first, second):
        async with a_client(database, roll, roll_once, environment=environment) as client:
            status = await client.run(roll_once, {"seed": Array.scalar("s", str)})
            assert status.state == "succeeded", status
            jobs.append(status.job_id)
    producer, reuser = jobs
    async with a_client(database, roll, roll_once) as client:
        for job, root_commit in ((producer, first.commit), (reuser, second.commit)):
            data = await _run(schema, client, PROVENANCE_QUERY, j=str(job), key="root/roll")
            invocation = data["invocation"]
            assert invocation["provenance"] == {"jobId": str(job), "commit": root_commit}
            (attempt,) = invocation["attempts"]
            assert attempt["state"] == "completed"
            assert attempt["provenance"]["jobId"] == str(job)
            assert attempt["provenance"]["commit"] == root_commit
            step = data["step"]
            assert step["provenance"] == {
                "jobId": str(producer),
                "jobAttemptId": step["provenance"]["jobAttemptId"],
                "commit": first.commit,
                "tree": first.tree,
                "ephemeral": False,
                "projectPath": ".",
                "environmentVerified": True,
            }
            assert step["attempt"]["provenance"] == {"jobId": str(producer), "commit": first.commit}
        (entry,) = (
            await _run(schema, client, "{ stepCache { attempt { provenance { jobId } } } }")
        )["stepCache"]
        assert entry["attempt"]["provenance"]["jobId"] == str(producer)


@pytest.mark.parametrize(
    ("query", "variables", "message"),
    [
        ("{ jobSummaries }", {}, "must have a selection"),
        ('{ levelOverview(jobId: "job", pathname: "root") { spawns } }', {}, "Cannot query field"),
        ('{ outputPage(source: {cid: "cid", typo: 1}) { total } }', {}, "not defined by type"),
        (
            'query($s: OutputSort) { outputPage(source: {cid: "cid"}, sort: $s) { total } }',
            {"s": {"key": "value:", "direction": "sideways"}},
            "SortDirection",
        ),
        (
            "query($s: OutputSource!) { outputPage(source: $s) { total } }",
            {"s": {"unknown": "cid"}},
            "not defined by type",
        ),
    ],
)
async def test_paging_contracts_reject_invalid_graphql(query, variables, message):
    # Validation must fail before a resolver needs a client or loads any rows.
    _, result = await graphql(build_schema(), {"query": query, "variables": variables})
    assert any(message in error["message"] for error in result["errors"]), result


# ---- the view bundle: GET /api/views/{name} ----


def a_views_project(root: Path, views: bool = True) -> Project:
    """A project (no database needed) whose renderer package is ``root/views``."""
    return Project(
        root=root,
        name="p",
        modules=(),
        stale_after=timedelta(seconds=30),
        database_url="unused",
        schema="unused",
        views=ViewsConfig(package=root / "views") if views else None,
    )


def a_built_bundle(
    root: Path, *, stylesheet: bool = True, api_version: int = VIEW_API_VERSION
) -> Path:
    dist = root / "views" / "dist"
    dist.mkdir(parents=True)
    (dist / "bundle.js").write_text("export default () => ({ renderers: [] });")
    manifest: dict[str, Any] = {"apiVersion": api_version, "module": "bundle.js"}
    if stylesheet:
        (dist / "bundle.css").write_text(".a {}")
        manifest["stylesheet"] = "bundle.css"
    (dist / VIEW_BUNDLE_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    (dist / VIEW_BUNDLE_SOURCES_FILENAME).write_text(
        json.dumps({"sources": sources_fingerprint(ViewsConfig(package=root / "views"))})
    )
    return dist


async def refresh_views(project: Project) -> ViewsWatcher:
    assert project.views is not None
    views = ViewsWatcher(project.views)
    await views.refresh()
    return views


async def test_the_bundle_is_served_file_by_file_uncached(tmp_path: Path) -> None:
    a_built_bundle(tmp_path)
    project = a_views_project(tmp_path)
    for name, media_type, body in [
        (VIEW_BUNDLE_MANIFEST_FILENAME, "application/json", b'"module": "bundle.js"'),
        ("bundle.js", "text/javascript", b"renderers"),
        ("bundle.css", "text/css", b".a {}"),
    ]:
        response = views_response(await refresh_views(project), name)
        assert response.status_code == 200, name
        assert response.media_type == media_type
        assert body in bytes(response.body)
        assert response.headers["cache-control"] == "no-cache"


async def test_only_the_files_the_manifest_names_are_served(tmp_path: Path) -> None:
    dist = a_built_bundle(tmp_path, stylesheet=False)
    (dist / "other.js").write_text("")
    project = a_views_project(tmp_path)
    for name in ("other.js", "bundle.css", "package.json"):
        response = views_response(await refresh_views(project), name)
        assert response.status_code == 404, name
        assert response.headers["cache-control"] == "no-cache"


async def test_a_project_without_views_has_no_bundle(tmp_path: Path) -> None:
    response = views_response(None, VIEW_BUNDLE_MANIFEST_FILENAME)
    assert response.status_code == 404


async def test_a_missing_or_invalid_bundle_says_why(tmp_path: Path) -> None:
    project = a_views_project(tmp_path)
    missing = views_response(await refresh_views(project), VIEW_BUNDLE_MANIFEST_FILENAME)
    assert missing.status_code == 409
    assert b"has no package.json" in bytes(missing.body)
    a_built_bundle(tmp_path, api_version=VIEW_API_VERSION + 1)
    stale = views_response(await refresh_views(project), "bundle.js")
    assert stale.status_code == 409
    assert b"has no package.json" in bytes(stale.body)


async def test_the_manifest_names_the_served_version(tmp_path: Path) -> None:
    dist = a_built_bundle(tmp_path)
    current = views_response(
        await refresh_views(a_views_project(tmp_path)), VIEW_BUNDLE_MANIFEST_FILENAME
    )
    assert current.status_code == 200
    assert current.headers[VIEWS_VERSION_HEADER] == bundle_version(dist)


async def test_startup_leaves_a_current_bundle_and_survives_a_failed_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = a_built_bundle(tmp_path)
    views = ViewsConfig(package=tmp_path / "views")
    (dist / VIEW_BUNDLE_SOURCES_FILENAME).write_text(
        json.dumps({"sources": sources_fingerprint(views)})
    )
    watcher = ViewsWatcher(views)
    await watcher.refresh()
    assert capsys.readouterr().out == ""
    assert watcher.state.state == "ready" and watcher.state.version is not None
    # Stale, and the build cannot run (no package.json): reported, and the bundle stays.
    (tmp_path / "views" / "index.ts").write_text("changed")
    await watcher.refresh()
    out = capsys.readouterr().out
    assert "building the project's view bundle (its sources changed" in out
    assert "could not build the project's view bundle" in out
    assert "serving the previous one" in out
    assert (dist / "bundle.js").is_file()
    assert watcher.state.state == "failed" and "has no package.json" in (watcher.state.error or "")


# ---- code links and the experiment: GET /api/source/{name}, GET /api/experiment_readme ----


@pytest.mark.parametrize("function", [roll, roll_once])
@pytest.mark.parametrize(
    "name", ["demo.roll", "demo/roll", "/demo/roll/", "demo//roll", "demo/%2F/roll"]
)
async def test_source_route_preserves_registered_function_names(
    function: Function,
    name: str,
) -> None:
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        messages.append(message)

    # No connection is opened: source lookups only need the client's registry.
    async with a_client(
        Database("postgresql+asyncpg://unused", pg_schema="unused"),
        dataclasses.replace(function, name=name),
    ) as client:
        url = f"/api/source/{quote(name, safe='')}"
        await create_app(client)(
            {
                "type": "http",
                "method": "GET",
                # ASGI servers decode percent-encoded paths before route matching.
                "path": unquote(url),
                "raw_path": url.encode(),
                "root_path": "",
                "query_string": b"",
                "headers": [],
            },
            receive,
            send,
        )

    assert messages[0]["status"] == 200
    source = json.loads(b"".join(m.get("body", b"") for m in messages))
    assert source["symbol"] == function.body.__qualname__
    assert Path(source["repoRoot"]) / source["file"] == Path(__file__).resolve()


def test_a_registered_function_says_where_it_is_implemented() -> None:
    registry = FunctionRegistry([roll, roll_once])
    response = source_response(registry, roll.name)
    assert response.status_code == 200
    source = json.loads(bytes(response.body))
    here = Path(__file__).resolve()
    assert Path(source["repoRoot"]) / source["file"] == here
    assert here.is_relative_to(source["repoRoot"]) and source["repoRoot"] != str(here.parent)
    assert source["symbol"] == "roll"
    lines = here.read_text().splitlines()
    # The range starts at the decorator and ends at the body's last line.
    assert lines[source["startLine"] - 1].startswith("@step(")
    assert lines[source["endLine"] - 1] == "    return 42"
    assert source["docstring"] == ""


async def test_code_links_follow_the_code_version_the_job_ran(database: Database) -> None:
    async with _completed_job(database) as (client, job):
        from fxtr.runner.postgres.debug_web.browse import level_overview

        overview = await level_overview(client, job, "root")
        assert overview is not None and overview["codeVersion"] is not None
        recorded = overview["codeVersion"]
        code_version = await client.entities.load(
            EntityID(cid=cid_from_str(recorded)), as_type=CodeVersion
        )
        source = code_version.functions[roll.name]
        assert isinstance(source, RepoSource)
        committed = (
            "\n" * (source.start_line - 1)
            + '@step()\ndef roll():\n    """Rolls, as committed."""\n'
        ).encode()
        environment = FakeEnvironment(
            committed_files={(code_version.commit, source.file): committed}
        )
        # The viewer's registry has no `roll`: it was imported before the step was written.
        async with a_client(database, environment=environment) as viewer:
            app = create_app(viewer)
            status, body = await _get(app, f"/api/source/{roll.name}", f"codeVersion={recorded}")
            assert status == 200
            assert json.loads(body) == {
                "file": source.file,
                "repoRoot": str(enclosing_repository(viewer.project.root.resolve())),
                "symbol": source.symbol,
                "startLine": source.start_line,
                "endLine": source.end_line,
                "docstring": "Rolls, as committed.",
                "commit": code_version.commit,
            }
            # Without a code version, the viewer's registry is all there is to ask.
            status, _ = await _get(app, f"/api/source/{roll.name}", "")
            assert status == 404
            status, _ = await _get(app, f"/api/source/{roll.name}", "codeVersion=nonsense")
            assert status == 400


async def _get(app: ASGIApp, path: str, query: str) -> tuple[int, bytes]:
    """One GET through ``app``: the status and the body."""
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": query.encode(),
            "headers": [],
        },
        receive,
        send,
    )
    return messages[0]["status"], b"".join(m.get("body", b"") for m in messages)


async def test_files_are_served_from_the_repository_at_a_commit_or_the_working_tree() -> None:
    environment = FakeEnvironment(committed_files={("abc1234", "tests/x.py"): b"at commit\n"})
    # No connection is opened: files need only the project and the environment.
    async with a_client(
        Database("postgresql+asyncpg://unused", pg_schema="unused"), environment=environment
    ) as viewer:
        app = create_app(viewer)
        repo_root = enclosing_repository(viewer.project.root.resolve())
        assert repo_root is not None
        here = Path(__file__).resolve().relative_to(repo_root).as_posix()

        status, body = await _get(app, "/api/file", "path=tests/x.py&commit=abc1234")
        assert status == 200
        assert json.loads(body) == {
            "path": "tests/x.py",
            "commit": "abc1234",
            "text": "at commit\n",
        }
        status, body = await _get(app, "/api/file", f"path={here}")
        assert status == 200
        assert json.loads(body)["commit"] is None
        assert json.loads(body)["text"] == Path(__file__).read_text()
        assert (await _get(app, "/api/file", "path=tests/y.py&commit=abc1234"))[0] == 404
        assert (await _get(app, "/api/file", "path=tests/x.py&commit=--output=x"))[0] == 400
        for outside in ("/etc/passwd", "../outside.py", "a/../../outside.py", ""):
            assert (await _get(app, "/api/file", f"path={outside}"))[0] == 404


def test_a_file_path_must_stay_inside_the_repository(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "link").symlink_to(tmp_path.parent)
    assert file_request_path(tmp_path, "src/a.py") == "src/a.py"
    assert file_request_path(tmp_path, "src/link/escape.py") is None
    assert file_request_path(tmp_path, "../a.py") is None
    assert file_request_path(tmp_path, "/abs.py") is None


def test_a_docstring_is_found_by_where_its_definition_starts() -> None:
    module = b'''import x

@first
@second(1)
async def decorated():
    """Line one.

        Indented.
    """

class Plain:
    def method(self):
        pass
'''
    assert docstring_at(module, 3) == "Line one.\n\nIndented."
    assert docstring_at(module, 5) == ""  # the def line, not where the definition starts
    assert docstring_at(module, 11) == ""
    assert docstring_at(module, 12) == ""
    assert docstring_at(b"def broken(:\n", 1) == ""


def test_an_unregistered_function_has_no_source() -> None:
    response = source_response(FunctionRegistry([roll]), "nowhere.to_be_found")
    assert response.status_code == 404
    assert b"No function named 'nowhere.to_be_found'" in bytes(response.body)


def test_the_experiment_readme_is_the_project_roots(tmp_path: Path) -> None:
    project = a_views_project(tmp_path, views=False)
    assert experiment_readme_response(project).status_code == 404
    (tmp_path / "EXPERIMENT.md").write_text("# p\n")
    response = experiment_readme_response(project)
    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {
        "path": str(tmp_path / "EXPERIMENT.md"),
        "markdown": "# p\n",
    }


# ---- the viewer itself: everything outside /api ----


async def _request(
    app: ASGIApp,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    *,
    method: str = "GET",
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    """One request through ``app``, without a server: the status, headers, and body."""
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": body}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": headers or [(b"host", b"127.0.0.1:8000")],
        },
        receive,
        send,
    )
    start = messages[0]
    response_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], response_headers, b"".join(m.get("body", b"") for m in messages[1:])


def a_built_viewer(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>viewer</title>")
    (dist / "assets" / "index.js").write_text("console.log(1)")
    return dist


async def test_the_viewer_is_served_from_the_root_uncached(tmp_path: Path) -> None:
    app = Starlette(routes=[Mount("/", ViewerFiles(a_built_viewer(tmp_path)))])
    status, headers, body = await _request(app, "/")
    assert (status, body) == (200, b"<!doctype html><title>viewer</title>")
    assert headers["cache-control"] == "no-cache"
    status, headers, body = await _request(app, "/assets/index.js")
    assert (status, body) == (200, b"console.log(1)")
    assert headers["cache-control"] == "no-cache"
    # A revalidation of an unchanged file is a 304 that still says to revalidate next time.
    _, headers, _ = await _request(app, "/")
    status, headers, _ = await _request(app, "/", [(b"if-none-match", headers["etag"].encode())])
    assert status == 304 and headers["cache-control"] == "no-cache"
    assert (await _request(app, "/nowhere.js"))[0] == 404


async def test_an_unbuilt_viewer_says_how_to_build_it(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    app = Starlette(routes=[Mount("/", ViewerFiles(dist))])
    status, headers, body = await _request(app, "/")
    assert status == 404 and b"pnpm --filter fxtr-ide build" in body
    assert headers["cache-control"] == "no-cache"
    # Building it while the server runs is enough.
    a_built_viewer(tmp_path)
    assert (await _request(app, "/"))[0] == 200


def test_an_installed_viewer_is_preferred_to_the_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged, checkout = tmp_path / "_frontend", tmp_path / "frontend" / "dist"
    monkeypatch.setattr(app_module, "PACKAGED_FRONTEND_DIST", packaged)
    monkeypatch.setattr(app_module, "CHECKOUT_FRONTEND_DIST", checkout)
    assert default_frontend_dist() == checkout
    packaged.mkdir()
    assert default_frontend_dist() == packaged


async def test_the_api_and_the_viewer_share_one_origin(tmp_path: Path) -> None:
    project = a_views_project(tmp_path, views=False)
    (tmp_path / "EXPERIMENT.md").write_text("# p")
    async with a_client(Database("postgresql+asyncpg://unused", pg_schema="unused")) as client:
        client.project = project
        app = create_app(client, frontend_dist=a_built_viewer(tmp_path))
        status, _, body = await _request(app, "/api/experiment_readme")
        assert status == 200 and json.loads(body)["markdown"] == "# p"
        assert (await _request(app, "/"))[0] == 200
        # The API is only under /api.
        assert (await _request(app, "/experiment_readme"))[0] == 404
        status, headers, _ = await _request(app, "/api/graphql")
        assert status == 307 and headers["location"].endswith("/api/graphql/")


async def test_the_server_says_which_project_it_serves(tmp_path: Path) -> None:
    project = a_views_project(tmp_path, views=False)
    async with a_client(Database("postgresql+asyncpg://unused", pg_schema="unused")) as client:
        client.project = project
        status, headers, body = await _request(create_app(client), "/api/info")
        assert (status, headers["cache-control"]) == (200, "no-cache")
        info = json.loads(body)
        assert (info["root"], info["schema"]) == (str(tmp_path.resolve()), "unused")
        assert isinstance(info["fxtrVersion"], str)


def a_project_dir(root: Path) -> Path:
    """A project directory ``fxtr view`` can be pointed at (it is never served here)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname = "p"\n\n[tool.fxtr]\nmodules = []\n')
    (root / "fxtr.local.toml").write_text('database_url = "postgresql+asyncpg://unused/db"\n')
    return root


@pytest.mark.parametrize("open_browser", [True, False])
async def test_a_second_fxtr_view_opens_the_running_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    open_browser: bool,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(server_module, "find_viewer", lambda project: "http://127.0.0.1:8001/")
    monkeypatch.setattr(server_module.webbrowser, "open", opened.append)
    with anyio.fail_after(10):
        await serve(a_project_dir(tmp_path / "p"), open_browser=open_browser)
    assert "the project's viewer is already running at http://127.0.0.1:8001/" in (
        capsys.readouterr().out
    )
    assert opened == (["http://127.0.0.1:8001/"] if open_browser else [])


def a_port_pair() -> int:
    """A port free on 127.0.0.1 whose dev server port is free too."""
    for port in random.sample(range(20000, 50000), 100):
        if free_port("127.0.0.1", port) and free_port("127.0.0.1", port + DEV_SERVER_PORT_OFFSET):
            return port
    raise AssertionError("no pair of free ports")


@pytest.fixture
def no_running_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_module, "find_viewer", lambda project: None)


@pytest.mark.usefixtures("no_running_viewer")
async def test_a_taken_port_is_refused_naming_what_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = a_project_dir(tmp_path / "p")
    port = a_port_pair()
    with socket.create_server(("127.0.0.1", port)):
        with pytest.raises(ViewerPortError, match=rf"port {port} is in use by another program"):
            await serve(project, port=port, open_browser=False)
        monkeypatch.setattr(
            ports_module, "viewer_info", lambda url: {"root": "/elsewhere", "schema": "s"}
        )
        with pytest.raises(ViewerPortError, match=r"the viewer of /elsewhere \(schema s\)"):
            await serve(project, port=port, open_browser=False)


@pytest.mark.usefixtures("no_running_viewer")
async def test_a_taken_dev_server_port_is_refused_in_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = a_project_dir(tmp_path / "p")
    port = a_port_pair()
    monkeypatch.setattr(server_module, "checkout_frontend", lambda: tmp_path / "frontend")
    with socket.create_server(("127.0.0.1", port + DEV_SERVER_PORT_OFFSET)):
        with pytest.raises(ViewerPortError, match="where the viewer's dev server runs.*--port"):
            await serve(project, port=port, open_browser=False)
    with pytest.raises(ViewerPortError, match="past the last port"):
        await serve(project, port=65000, open_browser=False)


async def test_fxtr_view_refuses_a_taken_port_with_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fxtr.project import cli

    async def refused(*args: Any, **kwargs: Any) -> None:
        raise ViewerPortError("port 8000 is in use by another program: stop it")

    monkeypatch.setattr(server_module, "serve", refused)
    assert await cli.main(["view"]) == cli.EXIT_REFUSED
    assert "port 8000 is in use by another program" in capsys.readouterr().err


@pytest.mark.parametrize(
    "host, url",
    [("127.0.0.1", "http://127.0.0.1:8000/"), ("::1", "http://[::1]:8000/")],
)
def test_the_viewer_url_names_the_bound_address(host: str, url: str) -> None:
    assert viewer_url(host, 8000) == url


async def test_a_running_dev_server_takes_over_the_viewers_paths(tmp_path: Path) -> None:
    viewer = Viewer(ViewerFiles(a_built_viewer(tmp_path)))
    app = Starlette(routes=[Mount("/", viewer)])
    viewer.dev_port = 5199
    status, headers, _ = await _request(app, "/assets/index.js", [(b"host", b"localhost:8000")])
    assert status == 307 and headers["location"] == "http://localhost:5199/assets/index.js"
    assert headers["cache-control"] == "no-cache"
    viewer.dev_port = None
    assert (await _request(app, "/"))[0] == 200


def test_only_a_checkout_runs_the_dev_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged, frontend = tmp_path / "_frontend", tmp_path / "frontend"
    monkeypatch.setattr(app_module, "PACKAGED_FRONTEND_DIST", packaged)
    monkeypatch.setattr(app_module, "CHECKOUT_FRONTEND_DIST", frontend / "dist")
    assert checkout_frontend() is None
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    assert checkout_frontend() == frontend
    packaged.mkdir()
    assert checkout_frontend() is None


FAKE_VITE = """\
import os, socket, sys
args = sys.argv[1:]
host, port = args[args.index("--host") + 1], int(args[args.index("--port") + 1])
open("backend.txt", "w").write(os.environ["FXTR_DEV_BACKEND"])
if os.environ.get("FAKE_VITE_FAIL"):
    sys.exit(1)
server = socket.create_server((host, port))
while True:
    server.accept()[0].close()
"""


def a_fake_frontend(root: Path) -> Path:
    """A ``frontend/`` whose ``vite`` listens where it is told and records its backend."""
    frontend = root / "frontend"
    bin_dir = frontend / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    vite = bin_dir / "vite"
    vite.write_text(f"#!{sys.executable}\n{FAKE_VITE}")
    vite.chmod(0o755)
    return frontend


def a_dev_server_config() -> DevServerConfig:
    port = free_port("127.0.0.1")
    assert port is not None
    return DevServerConfig(host="127.0.0.1", backend="http://127.0.0.1:8123/", port=port)


async def test_the_dev_server_runs_for_the_duration(tmp_path: Path) -> None:
    frontend = a_fake_frontend(tmp_path)
    config = a_dev_server_config()
    async with run_dev_server(frontend, config) as port:
        assert port is not None
        stream = await anyio.connect_tcp("127.0.0.1", port)
        await stream.aclose()
        assert (frontend / "backend.txt").read_text() == "http://127.0.0.1:8123/"
    with pytest.raises(OSError):
        await anyio.connect_tcp("127.0.0.1", port)


def test_the_dev_server_port_follows_the_viewers() -> None:
    assert dev_server_port(8000) == 8000 + DEV_SERVER_PORT_OFFSET
    assert dev_server_port(65535) is None


async def test_the_dev_server_runs_on_its_port_or_not_at_all(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    frontend = a_fake_frontend(tmp_path)
    config = a_dev_server_config()
    async with run_dev_server(frontend, config) as port:
        assert port == config.port
    # Again at once, as a restart would: the same port.
    async with run_dev_server(frontend, config) as port:
        assert port == config.port
    # Taken meanwhile: no dev server (the built viewer is served at the same URL), not another port.
    with socket.create_server(("127.0.0.1", config.port)):
        async with run_dev_server(frontend, config) as port:
            assert port is None
    assert f"port {config.port}, where the viewer's dev server runs, is in use" in caplog.text


async def test_a_dev_server_that_cannot_start_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = a_dev_server_config()
    async with run_dev_server(tmp_path / "frontend", config) as port:
        assert port is None
    assert "pnpm install" in caplog.text
    monkeypatch.setenv("FAKE_VITE_FAIL", "1")
    async with run_dev_server(a_fake_frontend(tmp_path), config) as port:
        assert port is None
    assert "did not start" in caplog.text


# ---- the checkout's built viewer: rebuilt when stale, refused when it cannot be ----


FAKE_VITE_BUILD = """\
import os, pathlib, sys
args = sys.argv[1:]
assert args[0] == "build", args
if os.environ.get("FAKE_VITE_BUILD_FAIL"):
    sys.exit(1)
out = pathlib.Path(args[args.index("--outDir") + 1])
out.mkdir(parents=True, exist_ok=True)
(out / "index.html").write_text("<!doctype html><title>rebuilt</title>")
"""


def a_frontend(root: Path, *, vite: bool = True) -> Path:
    """A ``frontend/`` with one source file, and (when ``vite``) a ``vite`` whose ``build``
    writes an ``index.html`` to its ``--outDir``, or fails under ``FAKE_VITE_BUILD_FAIL``."""
    frontend = root / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "package.json").write_text("{}")
    (frontend / "src" / "main.ts").write_text("export {}")
    if vite:
        bin_dir = frontend / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "vite").write_text(f"#!{sys.executable}\n{FAKE_VITE_BUILD}")
        (bin_dir / "vite").chmod(0o755)
    return frontend


def stamp(frontend: Path) -> None:
    """Record the frontend's current sources in its built viewer, as a build would."""
    (frontend / "dist" / VIEWER_SOURCES_FILENAME).write_text(
        json.dumps({"sources": viewer_sources_fingerprint(frontend)})
    )


def test_a_viewer_is_stale_unless_built_from_the_current_sources(tmp_path: Path) -> None:
    frontend = a_frontend(tmp_path)
    assert viewer_stale_reason(frontend) == "it is not built"
    a_built_viewer(frontend)
    assert "built outside `fxtr view`" in (viewer_stale_reason(frontend) or "")
    stamp(frontend)
    assert viewer_stale_reason(frontend) is None
    # A typecheck's incremental state, installed packages, and the build itself are not sources.
    (frontend / "tsconfig.tsbuildinfo").write_text("{}")
    (frontend / "node_modules" / "left-pad.js").write_text("")
    (frontend / "dist" / "extra.js").write_text("")
    assert viewer_stale_reason(frontend) is None
    (frontend / "src" / "main.ts").write_text("export const changed = 1")
    assert viewer_stale_reason(frontend) == "its sources changed since it was built"


async def test_a_build_records_its_sources_and_replaces_the_viewer(tmp_path: Path) -> None:
    frontend = a_frontend(tmp_path)
    a_built_viewer(frontend)
    await build_viewer(frontend)
    assert "rebuilt" in (frontend / "dist" / "index.html").read_text()
    assert not (frontend / "dist" / "assets").exists()  # the old build is gone, not merged
    assert viewer_stale_reason(frontend) is None
    assert list((frontend / "node_modules" / ".cache" / "fxtr-viewer").iterdir()) == []


async def test_a_failed_build_keeps_the_previous_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = a_frontend(tmp_path)
    a_built_viewer(frontend)
    monkeypatch.setenv("FAKE_VITE_BUILD_FAIL", "1")
    with pytest.raises(ViewerBuildError, match="failed"):
        await build_viewer(frontend)
    assert "<title>viewer</title>" in (frontend / "dist" / "index.html").read_text()
    with pytest.raises(ViewerBuildError, match="pnpm install"):
        await build_viewer(a_frontend(tmp_path / "bare", vite=False))


async def test_refreshing_rebuilds_a_stale_viewer_and_leaves_a_current_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    frontend = a_frontend(tmp_path)
    a_built_viewer(frontend)
    stamp(frontend)
    assert await refresh_viewer(frontend) is None
    assert capsys.readouterr().out == ""
    (frontend / "src" / "main.ts").write_text("export const changed = 1")
    assert await refresh_viewer(frontend) is None
    out = capsys.readouterr().out
    assert "building the viewer" in out and "its sources changed" in out
    assert "rebuilt" in (frontend / "dist" / "index.html").read_text()
    assert viewer_stale_reason(frontend) is None


async def test_a_stale_viewer_that_cannot_be_rebuilt_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    frontend = a_frontend(tmp_path, vite=False)
    a_built_viewer(frontend)
    refusal = await refresh_viewer(frontend)
    assert refusal is not None
    assert (
        "out of date" in refusal and "pnpm install" in refusal and "restart `fxtr view`" in refusal
    )
    assert refusal in capsys.readouterr().out
    assert (frontend / "dist" / "index.html").is_file()  # kept, though not served


async def test_a_refused_viewer_answers_every_path_with_why(tmp_path: Path) -> None:
    viewer = Viewer(ViewerFiles(a_built_viewer(tmp_path)))
    app = Starlette(routes=[Mount("/", viewer)])
    viewer.refusal = "The viewer is out of date."
    for path in ("/", "/assets/index.js"):
        status, headers, body = await _request(app, path)
        assert (status, body) == (503, b"The viewer is out of date.")
        assert headers["cache-control"] == "no-cache"
    viewer.refusal = None
    assert (await _request(app, "/"))[0] == 200


async def test_startup_refuses_a_checkouts_stale_viewer_but_keeps_the_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = a_frontend(tmp_path, vite=False)
    a_built_viewer(frontend)
    monkeypatch.setattr(app_module, "checkout_frontend", lambda: frontend)
    monkeypatch.setattr(app_module, "CHECKOUT_FRONTEND_DIST", frontend / "dist")
    (tmp_path / "EXPERIMENT.md").write_text("# p")
    client = a_client(Database("postgresql+asyncpg://unused", pg_schema="unused"))
    client.project = dataclasses.replace(client.project, root=tmp_path)
    app = create_app(client)
    async with app.router.lifespan_context(app):
        assert (await _request(app, "/"))[0] == 503
        assert (await _request(app, "/api/experiment_readme"))[0] == 200
