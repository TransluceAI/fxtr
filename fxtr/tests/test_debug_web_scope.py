"""The viewer backend's job scope: a reader allowed some of a database's jobs shows only them.

Two jobs run the same tiny workflow in one schema, the second reusing the first's step result
from the cache. A reader scoped to one of them lists only it, answers every job-keyed root query
for the other exactly as for a job that does not exist, shows the reused result without the
producer's attempt history, and serves only the entities ``job_entities`` names for its jobs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import anyio
import pytest
from postgres_testing import a_client

from fxtr.core import codec
from fxtr.core.entity_data import EntityID, cid_from_str
from fxtr.entity_defns.array import Array
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow

pytest.importorskip("sqlalchemy")
pytest.importorskip("ariadne")

from ariadne import graphql  # noqa: E402
from asgi_testing import a_request  # noqa: E402
from sqlalchemy import select  # noqa: E402

from fxtr.runner.postgres.client import LocalProjectClient  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.debug_web import JobScope, ViewerReader, create_reader_app  # noqa: E402
from fxtr.runner.postgres.debug_web.app import build_schema  # noqa: E402
from fxtr.runner.postgres.debug_web.output import OutputStore  # noqa: E402
from fxtr.runner.postgres.debug_web.scope import ROOT_FIELDS, reachable  # noqa: E402
from fxtr.runner.postgres.ledger import JobID  # noqa: E402
from fxtr.runner.postgres.schema import job_entities, jobs  # noqa: E402

pytestmark = pytest.mark.anyio

STEP_KEY = 'root/roll[seed:"s"]'
"""The cache key every job here requests: the one mapped call of the step named ``roll`` under
the root ``root``, for the one seed."""

SEEDS = Array.from_items([(("s",), "s")], ("[seed: str]", str))


@step(inputs={"seed": str}, output=int)
async def roll(context: StepContext, seed: str) -> int:
    context.log(f"rolling {seed}")
    return 42


@workflow(inputs={"seeds": ("[seed: str]", str)}, output=("[seed: str]", int))
async def roll_once(context: WorkflowContext, seeds: ArrayHandle[str]) -> ArrayHandle[int]:
    return context.run_step("roll", roll, {"seed": seeds}, map_over=["seed"])


@step(inputs={"seed": str, "salt": str}, output=int)
async def roll_salted(context: StepContext, seed: str, salt: str) -> int:
    return 43


@workflow(inputs={"seeds": ("[seed: str]", str), "salt": str}, output=("[seed: str]", int))
async def roll_once_salted(
    context: WorkflowContext, seeds: ArrayHandle[str], salt: ArrayHandle[str]
) -> ArrayHandle[int]:
    """A different request at the same cache key as ``roll_once``: the node is named ``roll``."""
    return context.run_step("roll", roll_salted, {"seed": seeds, "salt": salt}, map_over=["seed"])


@dataclass(frozen=True)
class TwoJobs:
    """Two completed jobs in one schema, and the entities ``job_entities`` names for them.

    Attributes:
        client: The client that ran them, still entered.
        producer: The job that ran the step.
        reuser: The job that took the producer's step result from the cache.
        result: The cid of both jobs' result (the same array), named for both.
        only_producer: The cid of an entity named for the producer alone.
        only_reuser: The cid of an entity named for the reuser alone.
    """

    client: LocalProjectClient
    producer: JobID
    reuser: JobID
    result: str
    only_producer: str
    only_reuser: str


@asynccontextmanager
async def two_jobs(database: Database) -> AsyncIterator[TwoJobs]:
    """Run ``roll_once`` twice with the same input, so the second job's step is a cache hit, and
    fill ``job_entities`` for both as a record import would."""
    async with a_client(database, roll, roll_once, roll_salted, roll_once_salted) as client:
        job_ids: list[JobID] = []
        for _ in range(2):
            with anyio.fail_after(15):
                status = await client.run(roll_once, {"seeds": SEEDS})
            assert status.state == "succeeded", status
            job_ids.append(status.job_id)
        producer, reuser = job_ids
        result = (await client.jobs.status(reuser)).result
        assert result is not None
        extras = [(await client.entities.store(Array.scalar(n, int))).entity_id for n in (1, 2)]
        async with database.transaction() as tx:
            await client.entities.flush(tx)
            await tx.execute(
                job_entities.insert(),
                [
                    {"job_id": producer, "cid": result.entity_id.cid},
                    {"job_id": reuser, "cid": result.entity_id.cid},
                    {"job_id": producer, "cid": extras[0].cid},
                    {"job_id": reuser, "cid": extras[1].cid},
                ],
            )
        yield TwoJobs(
            client,
            producer,
            reuser,
            result.entity_id.cid_str(),
            extras[0].cid_str(),
            extras[1].cid_str(),
        )


def a_reader(database: Database, *job_ids: JobID) -> ViewerReader:
    return ViewerReader(database, stale_after=timedelta(minutes=5), scope=JobScope.of(job_ids))


async def answer(reader: ViewerReader, query: str, **variables: Any) -> tuple[Any, list[str]]:
    """One GraphQL query through the resolvers: its data (``None`` when an error voided it), and
    its errors' messages."""
    outputs = OutputStore(reader.database, reader.scope)
    try:
        _, result = await graphql(
            build_schema(),
            {"query": query, "variables": variables},
            context_value={"reader": reader, "outputs": outputs},
        )
    finally:
        outputs.close()
    return result.get("data"), [error["message"] for error in result.get("errors", [])]


async def data(reader: ViewerReader, query: str, **variables: Any) -> Any:
    found, errors = await answer(reader, query, **variables)
    assert not errors, errors
    return found


def test_a_scope_is_a_set_of_jobs_or_everything() -> None:
    one, other = JobID(uuid.uuid4()), JobID(uuid.uuid4())
    assert JobScope.all().unrestricted and JobScope.all().allows(one)
    scoped = JobScope.of([one])
    assert not scoped.unrestricted and scoped.allows(one) and not scoped.allows(other)
    assert not JobScope.of([]).allows(one)
    assert JobScope.of([one]) == JobScope.of({one})
    assert str(JobScope.all().restrict(jobs.c.id)) == "true"
    assert "IN" in str(scoped.restrict(jobs.c.id))


def test_every_root_field_has_a_scope_treatment() -> None:
    """A new root field must say how the scope applies to it (the table in ``scope.py``)."""
    query_type = build_schema().query_type
    assert query_type is not None
    assert set(query_type.fields) == set(ROOT_FIELDS)


async def test_a_scoped_reader_lists_only_its_jobs(database: Database) -> None:
    async with two_jobs(database) as pair:
        reader = a_reader(database, pair.reuser)
        assert [job["id"] for job in (await data(reader, "{ jobs { id } }"))["jobs"]] == [
            str(pair.reuser)
        ]
        page = (await data(reader, "{ jobSummaries { items { id state } next } }"))["jobSummaries"]
        assert [item["id"] for item in page["items"]] == [str(pair.reuser)]
        assert page["items"][0]["state"] == "succeeded" and page["next"] is None
        summary = "query($id: ID!) { jobSummary(id: $id) { id } }"
        assert (await data(reader, summary, id=str(pair.producer)))["jobSummary"] is None
        assert (await data(reader, summary, id=str(pair.reuser)))["jobSummary"] == {
            "id": str(pair.reuser)
        }
        everything = ViewerReader(database, stale_after=timedelta(minutes=5), scope=JobScope.all())
        assert {job["id"] for job in (await data(everything, "{ jobs { id } }"))["jobs"]} == {
            str(pair.producer),
            str(pair.reuser),
        }
        assert JobScope.of([]).allows(pair.reuser) is False
        assert (await data(a_reader(database), "{ jobs { id } }"))["jobs"] == []


async def test_the_job_pickers_reads_keep_to_the_scope(database: Database) -> None:
    """The job picker names workflows, reads summaries by id, and counts a job's reused results.
    Each keeps to the scope, and a summary compares its job only with jobs in the scope."""
    async with two_jobs(database) as pair:
        everything = ViewerReader(database, stale_after=timedelta(minutes=5), scope=JobScope.all())
        only_producer = a_reader(database, pair.producer)
        by_ids = "query($ids: [ID!]!) { jobSummariesByIds(ids: $ids) { id latest } }"
        ids = [str(pair.producer), str(pair.reuser)]
        # The reuser was created after the producer, so only a reader that may show it says the
        # producer is not the latest job of the workflow.
        assert (await data(everything, by_ids, ids=ids))["jobSummariesByIds"] == [
            {"id": str(pair.producer), "latest": False},
            {"id": str(pair.reuser), "latest": True},
        ]
        assert (await data(only_producer, by_ids, ids=ids))["jobSummariesByIds"] == [
            {"id": str(pair.producer), "latest": True}
        ]
        assert (await data(only_producer, "{ jobWorkflows }"))["jobWorkflows"] == [
            "test_debug_web_scope.roll_once"
        ]
        assert (await data(a_reader(database), "{ jobWorkflows }"))["jobWorkflows"] == []
        reuse = "query($id: ID!) { jobReuse(jobId: $id) { ran reused } }"
        assert (await data(everything, reuse, id=str(pair.reuser)))["jobReuse"] == [
            {"ran": 0, "reused": 1}
        ]
        assert (await data(only_producer, reuse, id=str(pair.reuser)))["jobReuse"] == []
        assert (await data(only_producer, reuse, id=str(pair.producer)))["jobReuse"] == [
            {"ran": 1, "reused": 0}
        ]


async def node_id(reader: ViewerReader, job: JobID) -> str:
    overview = (
        await data(
            reader,
            'query($j: ID!) { levelOverview(jobId: $j, pathname: "root")'
            " { nodes { identity counts { completed } } } }",
            j=str(job),
        )
    )["levelOverview"]
    return next(node["identity"] for node in overview["nodes"] if node["counts"] is not None)


JOB_KEYED_QUERIES: list[tuple[str, Callable[[str, str], dict[str, Any]]]] = [
    ("query($j: ID!) { job(id: $j) { id } }", lambda j, n: {"j": j}),
    (
        'query($j: ID!) { levelOverview(jobId: $j, pathname: "root") { pathname } }',
        lambda j, n: {"j": j},
    ),
    (
        'query($j: ID!, $n: String!) { runPage(jobId: $j, pathname: "root", nodeId: $n,'
        " selection: {}) { total } }",
        lambda j, n: {"j": j, "n": n},
    ),
    (
        'query($j: ID!, $n: String!) { keyPage(jobId: $j, pathname: "root", nodeId: $n,'
        ' dim: "seed", selection: {}) { total } }',
        lambda j, n: {"j": j, "n": n},
    ),
    (
        'query($j: ID!, $n: String!) { invocationPage(jobId: $j, pathname: "root", nodeId: $n,'
        " selection: {}) { total } }",
        lambda j, n: {"j": j, "n": n},
    ),
    ('query($j: ID!) { invocation(jobId: $j, pathname: "root") { id } }', lambda j, n: {"j": j}),
    ('query($j: ID!) { invocationVersion(jobId: $j, pathname: "root") }', lambda j, n: {"j": j}),
    (
        "query($j: ID!, $k: String!) { step(jobId: $j, cacheKey: $k) { id } }",
        lambda j, n: {"j": j, "k": STEP_KEY},
    ),
    (
        "query($s: OutputSource!) { outputPage(source: $s) { total } }",
        lambda j, n: {"s": {"jobId": j, "pathname": "root", "nodeId": n}},
    ),
    (
        'query($s: OutputSource!) { outputDimValues(source: $s, dim: "seed") { total } }',
        lambda j, n: {"s": {"jobId": j, "pathname": "root", "nodeId": n}},
    ),
]
"""Every root field keyed by a job, with its variables for a job id and a run node identity."""


def nothing(found: Any) -> bool:
    """Whether a query's data holds nothing: absent, or every field null."""
    return found is None or all(value is None for value in found.values())


async def test_job_keyed_queries_answer_as_for_a_missing_job_outside_the_scope(
    database: Database,
) -> None:
    async with two_jobs(database) as pair:
        reader = a_reader(database, pair.reuser)
        node = await node_id(reader, pair.reuser)
        unknown = str(uuid.uuid4())
        for query, variables in JOB_KEYED_QUERIES:
            own = await answer(reader, query, **variables(str(pair.reuser), node))
            assert own[1] == [] and all(value is not None for value in own[0].values()), (
                query,
                own,
            )
            hidden = await answer(reader, query, **variables(str(pair.producer), node))
            missing = await answer(reader, query, **variables(unknown, node))
            # Not found: the same null, or the same error, as for a job that does not exist
            # (GraphQL keeps the null beside an error on a nullable field, and drops the data
            # for an error on a non-null one).
            assert nothing(hidden[0]), (query, hidden)
            assert (hidden[0], [m.replace(str(pair.producer), unknown) for m in hidden[1]]) == (
                missing
            ), (query, hidden, missing)


STEP_QUERY = """
query($j: ID!, $k: String!) {
  step(jobId: $j, cacheKey: $k) {
    state resultCid provenance { jobId }
    attempt { state provenance { jobId } logs { message } }
    logs { message }
  }
  invocation(jobId: $j, pathname: "root") {
    spawns { stepCacheKey state resultCid }
    attempts { state provenance { jobId } }
  }
  stepCache { cacheKey state provenance { jobId } attempt { state logs { message } } }
}
"""


async def test_a_reused_result_shows_without_the_producers_history(database: Database) -> None:
    """The reuser's step is bound to the producer's step result: with the producer outside the
    scope, the result, its state, and who produced it show, but not the producer's attempt or its
    logs; with the producer in the scope, they do."""
    async with two_jobs(database) as pair:
        reuser_only = await data(
            a_reader(database, pair.reuser), STEP_QUERY, j=str(pair.reuser), k=STEP_KEY
        )
        pin = reuser_only["step"]
        assert pin["state"] == "completed" and pin["resultCid"] is not None
        assert pin["provenance"] == {"jobId": str(pair.producer)}
        assert pin["attempt"] is None and pin["logs"] == []
        invocation = reuser_only["invocation"]
        assert invocation["spawns"] == [
            {"stepCacheKey": STEP_KEY, "state": "completed", "resultCid": pin["resultCid"]}
        ]
        # The invocation's own attempts are the reuser's, shown as ever.
        assert [a["provenance"]["jobId"] for a in invocation["attempts"]] == [str(pair.reuser)]
        (entry,) = reuser_only["stepCache"]
        assert entry["cacheKey"] == STEP_KEY and entry["state"] == "completed"
        assert entry["provenance"] == {"jobId": str(pair.producer)} and entry["attempt"] is None

        both = await data(
            a_reader(database, pair.producer, pair.reuser),
            STEP_QUERY,
            j=str(pair.reuser),
            k=STEP_KEY,
        )
        assert both["step"]["attempt"] == {
            "state": "completed",
            "provenance": {"jobId": str(pair.producer)},
            "logs": [{"message": "rolling s"}],
        }
        assert both["step"]["logs"] == [{"message": "rolling s"}]
        (entry,) = both["stepCache"]
        assert entry["attempt"] == {"state": "completed", "logs": [{"message": "rolling s"}]}

        # The producer sees its own attempt; a scope without either job sees no cache entry.
        producer_only = await data(
            a_reader(database, pair.producer), STEP_QUERY, j=str(pair.producer), k=STEP_KEY
        )
        assert producer_only["step"]["attempt"]["logs"] == [{"message": "rolling s"}]
        assert producer_only["stepCache"][0]["attempt"] is not None
        assert (await data(a_reader(database), "{ stepCache { cacheKey } }"))["stepCache"] == []


async def test_a_cache_entry_another_jobs_request_took_is_hidden(database: Database) -> None:
    """A third job overwrites the cache entry at the key with its own request's result: readers
    scoped to the first two jobs, whose bindings still name the first result, no longer see the
    entry (its result and producer are the third job's), while the reuser's step is unchanged."""
    async with two_jobs(database) as pair:
        with anyio.fail_after(15):
            status = await pair.client.run(
                roll_once_salted,
                {"seeds": SEEDS, "salt": Array.scalar("x", str)},
                overwrite_cache_conflicts=True,
            )
        assert status.state == "succeeded", status
        third = status.job_id
        cache = "{ stepCache { cacheKey state provenance { jobId } attempt { state } } }"
        for scoped in ((pair.reuser,), (pair.producer, pair.reuser)):
            assert (await data(a_reader(database, *scoped), cache))["stepCache"] == []
        for scoped in ((third,), (pair.reuser, third)):
            (entry,) = (await data(a_reader(database, *scoped), cache))["stepCache"]
            assert entry["provenance"] == {"jobId": str(third)}
            assert entry["attempt"] == {"state": "completed"}
        pins = [
            (await data(a_reader(database, job), STEP_QUERY, j=str(job), k=STEP_KEY))["step"]
            for job in (pair.producer, pair.reuser)
        ]
        assert pins[1]["state"] == "completed" and pins[1]["resultCid"] == pins[0]["resultCid"]
        assert pins[1]["provenance"] == {"jobId": str(pair.producer)}
        assert pins[1]["attempt"] is None and pins[0]["attempt"] is not None
        (entry,) = (await data(a_reader(database, pair.producer, third), cache))["stepCache"]
        assert entry["provenance"] == {"jobId": str(third)}


ENTITY_QUERIES = """
query($a: String!, $b: String!, $r: String!) {
  producers: entity(cid: $a) { cid }
  reusers: entity(cid: $b) { cid }
  metas: entityMetas(cids: [$a, $b, $r]) { cid }
  array(cid: $b) { rowCount }
  outputPage(source: {cid: $b}) { total }
}
"""


async def test_entity_response_limit_counts_repeats_and_nulls(database: Database) -> None:
    async with two_jobs(database) as pair:
        reader = a_reader(database, pair.reuser)
        stored = await reader.entities.load_raw_bytes(EntityID(cid=cid_from_str(pair.result)))
        assert stored is not None
        expected = codec.encode([stored[0], None, stored[0]])
        app = create_reader_app(reader, max_entity_response_bytes=len(expected))
        async with app.router.lifespan_context(app):
            status, _, body = await a_request(
                app,
                "/api/entities",
                method="POST",
                body=json.dumps({"cids": [pair.result, pair.only_producer, pair.result]}).encode(),
            )
            assert status == 200 and body == expected


async def test_entity_response_limit_refuses_before_loading_contents(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with two_jobs(database) as pair:
        reader = a_reader(database, pair.reuser)

        async def unexpected_load(*args: object, **kwargs: object) -> None:
            pytest.fail("Oversized entity contents must not be loaded")

        monkeypatch.setattr(reader.entities, "load_raw_bytes_many", unexpected_load)
        app = create_reader_app(reader, max_entity_response_bytes=1)
        async with app.router.lifespan_context(app):
            assert (await a_request(app, f"/api/entity/{pair.result}"))[0] == 413
            assert (await a_request(app, f"/api/entity/{pair.only_producer}"))[0] == 404
            status, _, _ = await a_request(
                app,
                "/api/entities",
                method="POST",
                body=json.dumps({"cids": [pair.result] * 100}).encode(),
            )
            assert status == 413


async def test_entities_are_served_only_through_a_job_in_the_scope(database: Database) -> None:
    async with two_jobs(database) as pair:
        reader = a_reader(database, pair.reuser)
        missing = EntityID(cid=codec.compute_cid(codec.encode({"$type": "t", "n": 0}))).cid_str()
        app = create_reader_app(reader)
        async with app.router.lifespan_context(app):
            status, _, body = await a_request(app, f"/api/entity/{pair.result}")
            stored: Any = codec.decode(body)
            assert status == 200 and stored["rows"] == [["s", 42]]
            assert (await a_request(app, f"/api/entity/{pair.only_reuser}"))[0] == 200
            # Named for the producer alone: not served, exactly as an entity not stored.
            assert (await a_request(app, f"/api/entity/{pair.only_producer}"))[0] == 404
            assert (await a_request(app, f"/api/entity/{missing}"))[0] == 404
            cids = [pair.result, pair.only_producer, pair.only_reuser, missing, pair.result]
            status, _, body = await a_request(
                app,
                "/api/entities",
                method="POST",
                body=f'{{"cids": {cids!r}}}'.replace("'", '"').encode(),
            )
            assert status == 200
            served: Any = codec.decode(body)
            assert [item is not None for item in served] == [True, False, True, False, True]
            only_reuser: Any = codec.decode(served[2])
            assert served[0] == served[4] and only_reuser["rows"] == [[2]]

        found = await data(
            reader, ENTITY_QUERIES, a=pair.only_producer, b=pair.only_reuser, r=pair.result
        )
        assert found["producers"] is None and found["reusers"] == {"cid": pair.only_reuser}
        assert {meta["cid"] for meta in found["metas"]} == {pair.only_reuser, pair.result}
        assert found["array"] == {"rowCount": 1} and found["outputPage"] == {"total": 1}

        # A typed load or an output source naming an unreachable cid fails as a missing one does.
        for query in (
            "query($c: String!) { array(cid: $c) { rowCount } }",
            "query($c: String!) { outputPage(source: {cid: $c}) { total } }",
        ):
            hidden = await answer(reader, query, c=pair.only_producer)
            absent = await answer(reader, query, c=missing)
            assert nothing(hidden[0]) and hidden[1], (query, hidden)
            assert (hidden[0], [m.replace(pair.only_producer, missing) for m in hidden[1]]) == (
                absent
            ), (query, hidden, absent)

        # The unrestricted scope serves everything without consulting job_entities.
        everything = ViewerReader(database, stale_after=timedelta(minutes=5), scope=JobScope.all())
        found = await data(
            everything, ENTITY_QUERIES, a=pair.only_producer, b=pair.only_reuser, r=pair.result
        )
        assert found["producers"] == {"cid": pair.only_producer}
        assert len(found["metas"]) == 3
        async with database.transaction() as tx:
            named = (await tx.execute(select(job_entities.c.cid))).scalars().all()
            unnamed = cid_from_str(missing)
            assert await reachable(tx, JobScope.all(), [unnamed]) == {unnamed}
            assert await reachable(tx, JobScope.of([pair.producer]), [*named, unnamed]) == {
                cid_from_str(pair.result),
                cid_from_str(pair.only_producer),
            }
            assert await reachable(tx, JobScope.of([pair.producer]), []) == set()
