"""Tests for job records: building a job's record from the ledger, exporting it as a CAR, and
importing it into another project: what travels, what is refused, what an import records about
the entities a job reaches, the id that names a job's state, and resuming an imported job."""

from __future__ import annotations

import dataclasses
import re
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from postgres_testing import TEST_CODE_VERSION, FakeEnvironment, a_client, a_runner
from sqlalchemy import CheckConstraint, Table, delete, select, update
from sqlalchemy.dialects.postgresql import insert

from fxtr.core import codec
from fxtr.core.car import CarLimits, read_car, write_car
from fxtr.core.entity_data import get_entity_type_tag
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.project.client import (
    AdoptReport,
    ImportRefusedError,
    JobID,
    JobNotFoundError,
    JobRunningError,
)
from fxtr.project.execution_record import (
    EXPORTED_OWNER,
    ExecutionRecord,
    validate_execution_record,
)
from fxtr.project.job_record import (
    JOB_RECORD_TYPE,
    SUPPORTED_RECORD_FORMATS,
    JobRecord,
    RecordFileError,
    RecordLimitError,
    RecordLimits,
    read_record,
    validate_record,
)

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres import execution, job_records  # noqa: E402
from fxtr.runner.postgres.database import Database, any_of  # noqa: E402
from fxtr.runner.postgres.job_records import insert_execution_record  # noqa: E402
from fxtr.runner.postgres.ledger import NotFoundError  # noqa: E402
from fxtr.runner.postgres.schema import (  # noqa: E402
    attempt_events,
    attempts,
    entities,
    job_attempt_provenance,
    job_attempts,
    job_entities,
    jobs,
    node_names,
    step_results,
    workflow_invocations,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def elsewhere(database: Database) -> AsyncIterator[Database]:
    """A second project's database, on its own schema of the same server: where records go."""
    async with Database(database.url, pg_schema=f"test_{uuid.uuid4().hex}") as other:
        await other.create_schema()
        try:
            yield other
        finally:
            with anyio.fail_after(30):
                await other.drop_schema(lock_timeout="5s")


@step(inputs={"x": int}, output=int)
async def double(context: StepContext, x: int) -> int:
    return 2 * x


@workflow(inputs={"x": int}, output=int)
async def doubled(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("double", double, {"x": x})


FAIL = [False]
"""Whether ``flaky`` raises: a step that fails in one project and succeeds in another."""


@step(inputs={"x": int}, output=int)
async def flaky(context: StepContext, x: int) -> int:
    context.log("flaky attempt")
    if FAIL[0]:
        raise RuntimeError("not here")
    return x + 1


@workflow(inputs={"x": int}, output=int)
async def once_flaky(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("flaky", flaky, {"x": x})


async def exported(database: Database, path: Path, x: int = 3) -> tuple[JobID, JobID]:
    """Run a producer job and a reuser that hits its step in the cache, export the reuser to
    ``path``, and return both jobs."""
    async with a_client(database, double, doubled) as client:
        producer = await client.run(doubled, {"x": Array.scalar(x, int)})
        reuser = await client.run(doubled, {"x": Array.scalar(x, int)})
        await client.export_job(reuser.job_id, path)
    return producer.job_id, reuser.job_id


async def stopped_and_finished(database: Database, tmp_path: Path) -> tuple[JobID, Path, Path]:
    """A job stopped by a failing step, exported then (``earlier``), and again once resumed to
    success (``later``)."""
    earlier, later = tmp_path / "earlier.car", tmp_path / "later.car"
    FAIL[0] = True
    try:
        async with a_client(database, flaky, once_flaky) as client:
            job = (await client.run(once_flaky, {"x": Array.scalar(1, int)})).job_id
            await client.export_job(job, earlier)
    finally:
        FAIL[0] = False
    async with a_client(database, flaky, once_flaky) as client:
        assert (await (await client.resume(job)).wait()).state == "succeeded"
        await client.export_job(job, later)
    return job, earlier, later


def rewrite(path: Path, *, root: bytes | None = None, drop: bytes | None = None) -> None:
    """Rewrite a record file with another root, or without one of its blocks."""
    (old_root,), blocks = read_car(path)
    kept = [(cid, data) for cid, data in blocks.items() if cid != drop]
    write_car(path, [old_root if root is None else root], kept)


def write_record(path: Path, record: JobRecord, blocks: Mapping[bytes, bytes]) -> None:
    """Write ``record`` to ``path`` as a record file carrying ``blocks``."""
    encoded = codec.encode(DEFAULT_CONVERTER.unstructure_entity(record))
    root = codec.compute_cid(encoded)
    write_car(path, [root], [(root, encoded), *blocks.items()])


async def run_and_export(database: Database, path: Path) -> JobID:
    """Run ``doubled`` once, so its job has a workflow attempt and a step attempt with their
    events, export the job to ``path``, and return it."""
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        await client.export_job(job, path)
    return job


def history_of(path: Path) -> tuple[JobRecord, Mapping[bytes, bytes], ExecutionRecord]:
    """A record file's record, blocks, and execution history, which it must carry."""
    file = read_record(path)
    assert file.record.execution is not None
    return file.record, file.blocks, file.record.execution


def with_history(record: JobRecord, history: ExecutionRecord) -> JobRecord:
    return dataclasses.replace(record, execution=history)


async def test_a_job_exports_its_facts_and_everything_they_reach(
    database: Database, tmp_path: Path
) -> None:
    async with a_client(database, double, doubled) as client:
        producer = await client.run(doubled, {"x": Array.scalar(3, int)})
        reuser = await client.run(doubled, {"x": Array.scalar(3, int)})  # a cache hit
        assert producer.state == reuser.state == "succeeded"
        path = tmp_path / "job.car"
        root = await client.export_job(reuser.job_id, path)
        producer_attempts = (await client.jobs.status(producer.job_id)).job_attempts
    roots, blocks = read_car(path)
    assert roots == [root.cid]
    data = codec.decode(blocks[root.cid])
    record = DEFAULT_CONVERTER.structure(data, JobRecord)
    assert data["$type"] == JOB_RECORD_TYPE  # type: ignore[index]
    assert record.job.id == str(reuser.job_id)
    # The step result came from the producer's job attempt, whose fact travels with it.
    (step_result,) = record.step_results
    (produced_by,) = producer_attempts
    assert step_result.job_attempt_id == str(produced_by.id)
    assert {a.job_id for a in record.job_attempts} == {
        str(reuser.job_id),
        str(producer.job_id),
    }
    # Every entity the record reaches is a block of the file.
    reached = {ref.cid for ref in codec.iter_entity_ids(data)}  # type: ignore[arg-type]
    assert reached <= set(blocks)
    code_version = codec.decode(blocks[record.job_attempts[0].code_version.entity_id.cid])
    assert code_version["commit"] == TEST_CODE_VERSION.commit  # type: ignore[index]


async def test_only_a_settled_job_that_exists_is_exported(
    database: Database, tmp_path: Path
) -> None:
    async with a_client(database, double, doubled) as client:
        held = await client.jobs.create(doubled, {"x": Array.scalar(1, int)})
        await a_runner(database, held, double, doubled)  # takes the job, runs nothing
        with pytest.raises(JobRunningError, match="still running"):
            await client.export_job(held, tmp_path / "held.car")
        with pytest.raises(JobNotFoundError):
            await client.export_job(JobID(uuid.uuid4()), tmp_path / "missing.car")
    assert not (tmp_path / "held.car").exists() and not (tmp_path / "missing.car").exists()


async def test_an_exported_job_imports_into_another_project_as_it_was(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    async with a_client(database, double, doubled) as client:
        producer = await client.run(doubled, {"x": Array.scalar(3, int)})
        reuser = await client.run(doubled, {"x": Array.scalar(3, int)})  # a cache hit
        path = tmp_path / "job.car"
        await client.export_job(reuser.job_id, path)
        (produced_by,) = (await client.jobs.status(producer.job_id)).job_attempts
        expected = await client.jobs.result(reuser.job_id)
    async with a_client(elsewhere, double, doubled) as client:
        assert await client.import_job(path) == reuser.job_id
        status = await client.jobs.status(reuser.job_id)
        assert status.state == "succeeded" and len(status.job_attempts) == 1
        assert await client.jobs.result(reuser.job_id) == expected
        step = (await client.jobs.describe(reuser.job_id)).steps["root/double"]
        assert step.attempt is None and step.result_provenance is not None
        assert step.result_provenance.job_attempt.id == produced_by.id
        assert step.result_provenance.job_attempt.job_id == producer.job_id
        assert step.result_provenance.code_version.commit == TEST_CODE_VERSION.commit
        assert await client.list_cache_entries() == []  # importing adopts nothing


async def test_an_imported_jobs_results_are_adopted_and_reused(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    async with a_client(database, double, doubled) as client:
        producer = await client.run(doubled, {"x": Array.scalar(3, int)})
        path = tmp_path / "job.car"
        await client.export_job(producer.job_id, path)
        (produced_by,) = (await client.jobs.status(producer.job_id)).job_attempts
    async with a_client(elsewhere, double, doubled) as client:
        await client.import_job(path)
        assert await client.adopt_cache(producer.job_id, "other") == AdoptReport([], {})
        assert await client.adopt_cache(producer.job_id) == AdoptReport(["root/double"], {})
        assert await client.adopt_cache(producer.job_id) == AdoptReport(
            [], {"root/double": "already"}
        )
        # A new job here takes the adopted result: nothing runs, and its provenance leads to the
        # exporting project's job attempt.
        later = await client.run(doubled, {"x": Array.scalar(3, int)})
        assert later.state == "succeeded"
        step = (await client.jobs.describe(later.job_id)).steps["root/double"]
        assert step.attempt is not None and step.result_provenance is not None
        assert step.attempt.owner_job_attempt_id == produced_by.id
        assert step.result_provenance.job_attempt.id == produced_by.id
        with pytest.raises(JobNotFoundError):
            await client.adopt_cache(JobID(uuid.uuid4()))


async def test_a_file_that_is_not_a_whole_record_is_refused(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    path = tmp_path / "job.car"
    await exported(database, path)
    (root,), blocks = read_car(path)
    record = DEFAULT_CONVERTER.structure(codec.decode(blocks[root]), JobRecord)
    async with a_client(elsewhere, double, doubled) as client:
        # A root of another type.
        other = codec.encode({"$type": "test.NotARecord"})
        other_cid = codec.compute_cid(other)
        write_car(tmp_path / "other.car", [other_cid], [(other_cid, other)])
        with pytest.raises(ImportRefusedError, match="not a job record"):
            await client.import_job(tmp_path / "other.car")
        # A record with the right tag but the wrong shape.
        bad = codec.encode({"$type": JOB_RECORD_TYPE, "job": 1})
        bad_cid = codec.compute_cid(bad)
        write_car(tmp_path / "bad.car", [bad_cid], [(bad_cid, bad)])
        with pytest.raises(ImportRefusedError, match="malformed"):
            await client.import_job(tmp_path / "bad.car")
        # A record lacking an entity it reaches: its result.
        (invocation,) = record.invocations
        assert invocation.result is not None
        rewrite(path, drop=invocation.result.entity_id.cid)
        with pytest.raises(ImportRefusedError, match="which its record reaches"):
            await client.import_job(path)
        assert await client.list_jobs() == []


def a_spawn_without_a_target(record: JobRecord) -> JobRecord:
    (spawn,) = record.spawns
    return dataclasses.replace(record, spawns=(dataclasses.replace(spawn, step_cache_key=None),))


def a_spawn_with_two_targets(record: JobRecord) -> JobRecord:
    (spawn,) = record.spawns
    (invocation,) = record.invocations
    two = dataclasses.replace(spawn, child_invocation_id=invocation.id)
    return dataclasses.replace(record, spawns=(two,))


def a_node_of_an_unknown_invocation(record: JobRecord) -> JobRecord:
    node, *rest = record.nodes
    stray = dataclasses.replace(node, invocation_id=str(uuid.uuid4()))
    return dataclasses.replace(record, nodes=(stray, *rest))


def an_edge_to_a_missing_node(record: JobRecord) -> JobRecord:
    edge, *rest = record.node_inputs
    stray = dataclasses.replace(edge, input_identity=b"\x01" * 36)
    return dataclasses.replace(record, node_inputs=(stray, *rest))


def two_rows_for_one_node(record: JobRecord) -> JobRecord:
    return dataclasses.replace(record, nodes=(*record.nodes, record.nodes[0]))


def a_binding_to_an_unknown_result(record: JobRecord) -> JobRecord:
    (binding,) = record.step_bindings
    stray = dataclasses.replace(binding, step_result_id=str(uuid.uuid4()))
    return dataclasses.replace(record, step_bindings=(stray,))


def a_fact_of_an_unrelated_job(record: JobRecord) -> JobRecord:
    fact = record.job_attempts[0]
    stray = dataclasses.replace(fact, id=str(uuid.uuid4()), job_id=str(uuid.uuid4()))
    return dataclasses.replace(record, job_attempts=(*record.job_attempts, stray))


def no_root_invocation(record: JobRecord) -> JobRecord:
    return dataclasses.replace(record, job=dataclasses.replace(record.job, root="elsewhere"))


def a_root_of_another_workflow(record: JobRecord) -> JobRecord:
    root, *rest = record.invocations
    return dataclasses.replace(
        record, invocations=(dataclasses.replace(root, workflow_name="other"), *rest)
    )


def a_returned_node_the_record_lacks(record: JobRecord) -> JobRecord:
    root, *rest = record.invocations
    return dataclasses.replace(
        record, invocations=(dataclasses.replace(root, returned_node=b"\x02" * 36), *rest)
    )


def an_invocation_no_spawn_created(record: JobRecord) -> JobRecord:
    root = record.invocations[0]
    stray = dataclasses.replace(
        root,
        id=str(uuid.uuid4()),
        pathname="root/stray",
        returned_node=None,
        result=None,
        job_attempt_id=None,
        finished_at=None,
    )
    return dataclasses.replace(record, invocations=(*record.invocations, stray))


def a_result_from_a_missing_producer(record: JobRecord) -> JobRecord:
    (result,) = record.step_results
    kept = tuple(fact for fact in record.job_attempts if fact.id != result.job_attempt_id)
    return dataclasses.replace(record, job_attempts=kept)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (a_spawn_without_a_target, "exactly one target"),
        (a_spawn_with_two_targets, "exactly one target"),
        (a_node_of_an_unknown_invocation, "does not hold node of invocation"),
        (an_edge_to_a_missing_node, "does not hold an edge's input node"),
        (two_rows_for_one_node, "two rows for one node"),
        (a_binding_to_an_unknown_result, "does not hold the step result bound"),
        (a_fact_of_an_unrelated_job, "produced none of the record's step results"),
        (a_result_from_a_missing_producer, "does not hold job attempt fact"),
        (no_root_invocation, "does not hold the root invocation 'elsewhere'"),
        (a_root_of_another_workflow, "the root invocation runs 'other'"),
        (a_returned_node_the_record_lacks, "does not hold the node invocation"),
        (an_invocation_no_spawn_created, "a spawn that created invocation 'root/stray'"),
    ],
)
async def test_a_record_whose_rows_disagree_is_refused_without_a_database(
    database: Database,
    tmp_path: Path,
    mutate: Callable[[JobRecord], JobRecord],
    message: str,
) -> None:
    path = tmp_path / "job.car"
    await exported(database, path)
    record = read_record(path).record
    validate_record(record)  # an export of a cache hit, whose producer is another job's, is valid
    with pytest.raises(RecordFileError, match=message):
        validate_record(mutate(record))


@pytest.mark.parametrize("commit", ["HEAD", "*", "0" * 39, "A" * 40])
async def test_a_record_naming_a_commit_that_is_not_an_object_name_is_refused(
    database: Database, tmp_path: Path, commit: str
) -> None:
    """A pulled record's commits reach git as object names and in refspecs, where "HEAD" or "*"
    would mean something other than one commit."""
    path = tmp_path / "job.car"
    environment = FakeEnvironment(commit=commit, tree="0" * 40)
    async with a_client(database, double, doubled, environment=environment) as client:
        job = await client.run(doubled, {"x": Array.scalar(1, int)})
        await client.export_job(job.job_id, path)
    with pytest.raises(RecordFileError, match="not git object names"):
        read_record(path)


async def test_read_record_validates_and_refuses_what_passes_its_limits(
    database: Database, tmp_path: Path
) -> None:
    path = tmp_path / "job.car"
    await exported(database, path)
    file = read_record(path)
    assert file.format == JOB_RECORD_TYPE and file.format in SUPPORTED_RECORD_FORMATS
    # The same file, its record rewritten to have a spawn with no target, is refused on reading.
    data = DEFAULT_CONVERTER.unstructure_entity(a_spawn_without_a_target(file.record))
    encoded = codec.encode(data)
    root = codec.compute_cid(encoded)
    write_car(tmp_path / "bad.car", [root], [(root, encoded), *file.blocks.items()])
    with pytest.raises(RecordFileError, match="exactly one target"):
        read_record(tmp_path / "bad.car")
    with pytest.raises(RecordLimitError, match="bytes, over the limit of 1"):
        read_record(path, limits=RecordLimits(car=CarLimits(max_compressed_bytes=1)))
    # A record of a format no reader knows is refused, naming the formats it reads.
    unknown = {**data, "$type": "org.transluce.fxtr.project.JobRecord.v9"}
    encoded = codec.encode(unknown)
    root = codec.compute_cid(encoded)
    write_car(tmp_path / "v9.car", [root], [(root, encoded), *file.blocks.items()])
    with pytest.raises(RecordFileError, match="a format this fxtr reads"):
        read_record(tmp_path / "v9.car")


V0_1_FIXTURE = Path(__file__).parent / "fixtures" / "job_record_v0_1.car"
"""A record of format v0.1, written once and kept: job 7787a897 of workflow
``fixture_doubled`` on x = 21, which took its one step (``fixture_double``, 2 * x) from job
2f75b008's result. Reading it back catches a change that breaks compatibility with records of
this format already written."""


async def test_a_kept_v0_1_record_is_read_and_imported_as_it_was_written(
    database: Database,
) -> None:
    file = read_record(V0_1_FIXTURE)
    assert file.format == "org.transluce.fxtr.project.JobRecord.v0.1"
    assert file.root.cid_str() == "bafyreicyp2soikn374qzj7z367depyafiwi3ul22dktt3twkeimmeqgnua"
    record = file.record
    job_id = "7787a897-096c-49cc-9a89-03fa130e0dcf"
    assert (record.job.id, record.job.workflow_name, record.job.root) == (
        job_id,
        "fixture_doubled",
        "root",
    )
    (step_result,) = record.step_results
    producers = {fact.id: fact.job_id for fact in record.job_attempts}
    assert producers[step_result.job_attempt_id] == "2f75b008-0aa4-4549-ae20-77f4ce941018"
    assert file.commits() == [TEST_CODE_VERSION.commit]
    async with a_client(database) as client:
        assert await client.import_job(V0_1_FIXTURE) == JobID(uuid.UUID(job_id))
        assert (await client.jobs.status(JobID(uuid.UUID(job_id)))).state == "succeeded"
        assert (await client.jobs.result(JobID(uuid.UUID(job_id)))).item() == 42


async def test_an_execution_record_shows_elsewhere_why_a_job_stopped(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    FAIL[0] = True
    try:
        async with a_client(database, flaky, once_flaky) as client:
            job = (await client.run(once_flaky, {"x": Array.scalar(1, int)})).job_id
            await client.export_job(job, tmp_path / "job.car")
    finally:
        FAIL[0] = False
    _, _, record = history_of(tmp_path / "job.car")
    assert record.job_id == str(job) and {a.owner for a in record.job_attempts} == {EXPORTED_OWNER}
    assert {a.state for a in record.attempts} >= {"failed"}
    assert [line.message for line in record.logs] == ["flaky attempt"]
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(tmp_path / "job.car")
        await client.import_job(tmp_path / "job.car", update=True)
        await client.export_job(job, tmp_path / "roundtrip.car")
        assert history_of(tmp_path / "roundtrip.car")[2] == record
        step = (await client.jobs.describe(job)).steps["root/flaky"]
        assert step.attempt is not None and step.attempt.state == "failed"
        assert step.attempt.error is not None and step.attempt.error.message == "not here"
    # A running attempt is not something an execution record can carry.
    (failed, *_) = [a for a in record.attempts if a.state == "failed"]
    running = dataclasses.replace(record, attempts=(dataclasses.replace(failed, state="running"),))
    with pytest.raises(RecordFileError, match="still running"):
        validate_execution_record(running)


async def test_results_produced_at_a_commit_this_repository_lacks_are_refused(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    path = tmp_path / "job.car"
    await exported(database, path)
    environment = FakeEnvironment(known_commits=frozenset())
    async with a_client(elsewhere, double, doubled, environment=environment) as client:
        with pytest.raises(ImportRefusedError, match=TEST_CODE_VERSION.commit):
            await client.import_job(path)
        assert await client.list_jobs() == []


async def test_a_job_already_here_is_refused(database: Database, tmp_path: Path) -> None:
    path = tmp_path / "job.car"
    await exported(database, path)
    async with a_client(database, double, doubled) as client:
        with pytest.raises(ImportRefusedError, match="already in this project"):
            await client.import_job(path)


async def test_a_shared_fact_that_differs_is_refused_and_nothing_is_written(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """The producer's record carries the step result the reuser's also carries; if elsewhere's
    copy of it differs, the reuser's import is refused whole."""
    reused = tmp_path / "reuser.car"
    producer, reuser = await exported(database, reused)
    produced = tmp_path / "producer.car"
    async with a_client(database, double, doubled) as client:
        await client.export_job(producer, produced)
    async with a_client(elsewhere, double, doubled) as client:
        await client.import_job(produced)
        async with elsewhere.transaction() as tx:
            await tx.execute(
                update(step_results).values(
                    completed_at=step_results.c.completed_at - timedelta(seconds=1)
                )
            )
        with pytest.raises(ImportRefusedError, match="step_results .* differs"):
            await client.import_job(reused)
        assert [job.job_id for job in await client.list_jobs()] == [producer]


async def test_a_job_whose_producer_arrived_with_another_job_still_imports(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """Importing the reuser brings the producer's job attempt fact along; importing the producer
    afterwards finds that fact already there, equal, and adds the rest."""
    reused = tmp_path / "reuser.car"
    producer, reuser = await exported(database, reused)
    produced = tmp_path / "producer.car"
    async with a_client(database, double, doubled) as client:
        await client.export_job(producer, produced)
    async with a_client(elsewhere, double, doubled) as client:
        assert await client.import_job(reused) == reuser
        assert await client.import_job(produced) == producer
        assert (await client.jobs.status(producer)).state == "succeeded"


async def a_forged_reuser(
    database: Database, tmp_path: Path, *, same_id: bool = False
) -> tuple[JobID, Path, Path, str]:
    """A producer's record, and a reuser's record forged to name one more attempt of the
    producer (with a step result it made): a new attempt, or (``same_id``) one with the id of the
    producer's real attempt but another creation time. Returns the producer, the two files, and
    the producer's record id in ``database``."""
    reused = tmp_path / "reuser.car"
    producer, _ = await exported(database, reused)
    produced = tmp_path / "producer.car"
    async with a_client(database, double, doubled) as client:
        await client.export_job(producer, produced)
    async with database.transaction() as tx:
        producer_record = await job_records.record_cid(tx, producer)
    file = read_record(reused)
    (fact,) = [row for row in file.record.job_attempts if row.job_id == str(producer)]
    if same_id:
        added = dataclasses.replace(fact, created_at="2000-01-01T00:00:00.000000Z")
        job_attempts = tuple(row for row in file.record.job_attempts if row != fact)
    else:
        added = dataclasses.replace(fact, id=str(uuid.uuid4()))
        job_attempts = file.record.job_attempts
    result = dataclasses.replace(
        file.record.step_results[0], id=str(uuid.uuid4()), job_attempt_id=added.id
    )
    step_results = (*file.record.step_results, result) if not same_id else file.record.step_results
    forged = tmp_path / "forged.car"
    write_record(
        forged,
        dataclasses.replace(
            file.record, job_attempts=(*job_attempts, added), step_results=step_results
        ),
        file.blocks,
    )
    return producer, produced, forged, producer_record


async def test_another_jobs_record_cannot_add_an_attempt_to_a_job_here(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """A job's attempts are those its own record brought. A reuser's record that names another
    attempt of its producer imports, and the producer's record stays the one its owner made."""
    producer, produced, forged, producer_record = await a_forged_reuser(database, tmp_path)
    async with a_client(elsewhere, double, doubled) as client:
        await client.import_job(produced)
        await client.import_job(forged)
        assert await client.import_job(produced, update=True) == producer
    async with elsewhere.transaction() as tx:
        assert await job_records.record_cid(tx, producer) == producer_record


@pytest.mark.parametrize("same_id", [False, True], ids=["new attempt", "attempt with its id"])
async def test_a_job_imports_after_another_jobs_record_named_attempts_of_it(
    database: Database, elsewhere: Database, tmp_path: Path, same_id: bool
) -> None:
    """A job id is no secret: a record reaching this database before the job's own can name
    attempts of it that the job never had, or give one of its attempts other values. The job's own
    record is the authority on its attempts, so it imports and is exported as its owner made it."""
    producer, produced, forged, producer_record = await a_forged_reuser(
        database, tmp_path, same_id=same_id
    )
    async with a_client(elsewhere, double, doubled) as client:
        await client.import_job(forged)
        assert await client.import_job(produced) == producer
    async with elsewhere.transaction() as tx:
        assert await job_records.record_cid(tx, producer) == producer_record


async def test_an_import_that_overlaps_another_jobs_cannot_change_it(
    database: Database,
    elsewhere: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reuser's import that began before its producer's import committed, and writes after it,
    leaves the producer's record as its owner made it. The hub locks each pushed job, not the
    jobs a record names, so the two imports run at once."""
    producer, produced, forged, producer_record = await a_forged_reuser(database, tmp_path)
    started = anyio.Event()
    producer_committed = anyio.Event()

    async def import_forged() -> None:
        async with elsewhere.transaction() as tx:
            execute = tx.execute

            async def paused(*args: Any, **kwargs: Any) -> Any:
                result = await execute(*args, **kwargs)
                # After the import's first read, before anything it writes.
                if not started.is_set():
                    started.set()
                    await producer_committed.wait()
                return result

            monkeypatch.setattr(tx, "execute", paused)
            await job_records.insert_record(tx, read_record(forged))

    with anyio.fail_after(30):
        async with anyio.create_task_group() as group:
            group.start_soon(import_forged)
            await started.wait()
            async with elsewhere.transaction() as tx:
                await job_records.insert_record(tx, read_record(produced))
            producer_committed.set()
    async with elsewhere.transaction() as tx:
        assert await job_records.record_cid(tx, producer) == producer_record


async def test_a_job_cannot_claim_an_attempt_another_jobs_record_gave_its_producer(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """A reuser's record says which job ran its producer's attempt. A record of another job that
    claims that attempt as its own is refused, the reuser's record is unchanged, and the
    producer's own import still takes the attempt over."""
    reused = tmp_path / "reuser.car"
    producer, reuser = await exported(database, reused)
    produced = tmp_path / "producer.car"
    async with a_client(database, double, doubled) as client:
        await client.export_job(producer, produced)
    async with database.transaction() as tx:
        producer_record = await job_records.record_cid(tx, producer)
    genuine = read_record(produced)
    record = genuine.record
    assert record.execution is not None
    claimant = str(uuid.uuid4())
    claimed = tmp_path / "claim.car"
    write_record(
        claimed,
        dataclasses.replace(
            record,
            job=dataclasses.replace(record.job, id=claimant),
            job_attempts=tuple(
                dataclasses.replace(row, job_id=claimant) for row in record.job_attempts
            ),
            execution=dataclasses.replace(
                record.execution,
                job_id=claimant,
                job_attempts=tuple(
                    dataclasses.replace(row, job_id=claimant)
                    for row in record.execution.job_attempts
                ),
            ),
        ),
        genuine.blocks,
    )
    async with a_client(elsewhere, double, doubled) as client:
        await client.import_job(reused)
        async with elsewhere.transaction() as tx:
            reuser_record = await job_records.record_cid(tx, reuser)
        with pytest.raises(ImportRefusedError, match="disagree about which job ran it"):
            await client.import_job(claimed)
        assert await client.import_job(produced) == producer
    async with elsewhere.transaction() as tx:
        assert await job_records.record_cid(tx, reuser) == reuser_record
        assert await job_records.record_cid(tx, producer) == producer_record


async def test_the_first_record_to_name_an_attempt_decides_its_job(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """An attempt id belongs to one job here: the first record to name it says which. A later
    record naming another job for it is refused, even the import of the job it names, since this
    database cannot tell which record is right; the record that came first is unchanged."""
    reused = tmp_path / "reuser.car"
    producer, reuser = await exported(database, reused)
    produced = tmp_path / "producer.car"
    async with a_client(database, double, doubled) as client:
        await client.export_job(producer, produced)
    file = read_record(reused)
    other_job = str(uuid.uuid4())
    misnamed = tmp_path / "misnamed.car"
    write_record(
        misnamed,
        dataclasses.replace(
            file.record,
            job_attempts=tuple(
                dataclasses.replace(row, job_id=other_job) if row.job_id == str(producer) else row
                for row in file.record.job_attempts
            ),
        ),
        file.blocks,
    )
    async with a_client(elsewhere, double, doubled) as client:
        await client.import_job(misnamed)
        async with elsewhere.transaction() as tx:
            reuser_record = await job_records.record_cid(tx, reuser)
        with pytest.raises(ImportRefusedError, match="disagree about which job ran it"):
            await client.import_job(produced)
    async with elsewhere.transaction() as tx:
        assert await job_records.record_cid(tx, reuser) == reuser_record


async def test_a_job_owns_its_attempts_when_a_reusers_import_overlaps_its_own(
    database: Database,
    elsewhere: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reuser's import that writes the producer's attempt fact while the producer's own import
    runs leaves the attempt the producer's, numbered first."""
    reused = tmp_path / "reuser.car"
    producer, _ = await exported(database, reused)
    produced = tmp_path / "producer.car"
    async with a_client(database, double, doubled) as client:
        await client.export_job(producer, produced)
    own, reuser = read_record(produced), read_record(reused)
    # The entities are already here (as an earlier import can leave them), so neither import
    # waits on the other's uncommitted entity rows while this test holds the producer's open.
    async with elsewhere.transaction() as tx:
        await tx.execute(
            insert(entities),
            [
                {"cid": cid, "type_tag": get_entity_type_tag(codec.decode(data)), "data": data}
                for cid, data in {**own.blocks, **reuser.blocks}.items()
            ],
        )
    reached = anyio.Event()
    reuser_committed = anyio.Event()
    merge = job_records._merge_attempt_facts

    async def paused(tx: Any, job_id: JobID, record: JobRecord) -> None:
        if job_id == producer:
            reached.set()
            await reuser_committed.wait()
        await merge(tx, job_id, record)

    monkeypatch.setattr(job_records, "_merge_attempt_facts", paused)

    async def import_producer() -> None:
        async with elsewhere.transaction() as tx:
            await job_records.insert_record(tx, own)

    with anyio.fail_after(30):
        async with anyio.create_task_group() as group:
            group.start_soon(import_producer)
            await reached.wait()
            async with elsewhere.transaction() as tx:
                await job_records.insert_record(tx, reuser)
            reuser_committed.set()
    async with elsewhere.transaction() as tx:
        flags = (
            await tx.execute(
                select(job_attempt_provenance.c.from_other_job).where(
                    job_attempt_provenance.c.job_id == producer
                )
            )
        ).scalars()
        assert list(flags) == [False]
        assert [ja.seq for ja in await execution.list_job_attempts(tx, producer)] == [1]


async def test_an_imported_stopped_job_resumes_here(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    path = tmp_path / "stopped.car"
    FAIL[0] = True
    try:
        async with a_client(database, flaky, once_flaky) as client:
            stopped = await client.run(once_flaky, {"x": Array.scalar(1, int)})
            assert stopped.state == "stopped" and stopped.failed
            await client.export_job(stopped.job_id, path)
    finally:
        FAIL[0] = False
    async with a_client(elsewhere, flaky, once_flaky) as client:
        job = await client.import_job(path)
        status = await client.jobs.status(job)
        assert status.state == "stopped" and status.failed
        handle = await client.resume(job)
        assert (await handle.wait()).state == "succeeded"
        assert await client.jobs.result(job) == Array.scalar(2, int)
        # The new job attempt is numbered after the imported one.
        taken = (await client.jobs.status(job)).job_attempts
        assert {attempt.seq for attempt in taken} == {1, 2}


async def test_update_brings_a_job_up_to_date_from_a_later_record(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """A job stopped by a failure is imported, then finished where it came from; importing the
    later record with ``update`` extends the copy: its result, the new job attempt, nothing lost."""
    job, earlier, later = await stopped_and_finished(database, tmp_path)
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(earlier)
        assert (await client.jobs.status(job)).state == "stopped"
        with pytest.raises(ImportRefusedError, match="--update"):
            await client.import_job(later)
        assert await client.import_job(later, update=True) == job
        assert (await client.jobs.status(job)).state == "succeeded"
        assert await client.jobs.result(job) == Array.scalar(2, int)
        # Updating again from the same record changes nothing; the earlier one lacks nothing here.
        assert await client.import_job(later, update=True) == job
        with pytest.raises(ImportRefusedError, match="the record lacks"):
            await client.import_job(earlier, update=True)


async def entities_of(database: Database, job: JobID) -> set[bytes]:
    """The cids ``job_entities`` records against the job."""
    async with database.transaction() as tx:
        rows = await tx.execute(select(job_entities.c.cid).where(job_entities.c.job_id == job))
        return set(rows.scalars())


async def test_an_import_records_the_entities_the_job_reaches(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """``job_entities`` holds what a job's imported record reaches, for a viewer scoped to some
    jobs; an update adds what the later record reaches; a job run locally has no rows; and the
    table is not part of the record, so the job exports as it came."""
    job, earlier, later = await stopped_and_finished(database, tmp_path)
    assert await entities_of(database, job) == set()
    reached_earlier, reached_later = (
        set(read_record(earlier).blocks),
        set(read_record(later).blocks),
    )
    assert reached_earlier < reached_later  # the resume added the result
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(earlier)
        assert await entities_of(elsewhere, job) == reached_earlier
        await client.import_job(later, update=True)
        assert await entities_of(elsewhere, job) == reached_later
        again = tmp_path / "again.car"
        root = await client.export_job(job, again)
    assert root == read_record(later).root
    assert read_record(again).blocks == read_record(later).blocks


async def test_a_record_reaching_more_entities_than_one_statement_binds_imports(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """A real job reaches tens of thousands of entities; the driver binds at most 32,767
    parameters to one statement, so the import writes its rows as an executemany."""
    path = tmp_path / "job.car"
    await exported(database, path)
    file = read_record(path)
    extra: dict[bytes, bytes] = {}
    for n in range(17_000):
        data = codec.encode({"$type": "test/tiny", "n": n})
        extra[codec.compute_cid(data)] = data
    assert 2 * len(extra) > 32_767
    big = dataclasses.replace(file, blocks={**file.blocks, **extra})
    async with elsewhere.transaction() as tx:
        job = await job_records.insert_record(tx, big)
    assert await entities_of(elsewhere, job) == set(big.blocks)


async def test_record_cid_names_the_record_an_export_would_write(
    database: Database, tmp_path: Path
) -> None:
    path = tmp_path / "stopped.car"
    FAIL[0] = True
    try:
        async with a_client(database, flaky, once_flaky) as client:
            job = (await client.run(once_flaky, {"x": Array.scalar(1, int)})).job_id
            async with database.transaction(snapshot=True) as tx:
                before = await job_records.record_cid(tx, job)
                root = await job_records.export_job(tx, client.entities, job, path)
    finally:
        FAIL[0] = False
    assert before == root.cid_str() == read_record(path).root.cid_str()
    async with a_client(database, flaky, once_flaky) as client:
        assert (await (await client.resume(job)).wait()).state == "succeeded"
        async with database.transaction(snapshot=True) as tx:
            after = await job_records.record_cid(tx, job)
            with pytest.raises(NotFoundError):
                await job_records.record_cid(tx, JobID(uuid.uuid4()))
    assert after != before


async def test_update_refuses_a_job_held_here(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    job, earlier, later = await stopped_and_finished(database, tmp_path)
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(earlier)
        await a_runner(elsewhere, job, flaky, once_flaky)  # takes the job, runs nothing
        with pytest.raises(ImportRefusedError, match="held by a process"):
            await client.import_job(later, update=True)


async def test_update_refuses_a_job_that_went_its_own_way_here(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """Resumed here as well as where it came from: each has a job attempt the other lacks."""
    job, earlier, later = await stopped_and_finished(database, tmp_path)
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(earlier)
        assert (await (await client.resume(job)).wait()).state == "succeeded"
        with pytest.raises(ImportRefusedError, match="the record lacks"):
            await client.import_job(later, update=True)


async def test_replace_takes_a_record_that_an_update_refuses(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """Resumed on both sides, the record replaces the copy here. The job is then the record's, and
    the results of the attempts it lacks stay in the step cache for a new job."""
    job, earlier, later = await stopped_and_finished(database, tmp_path)
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(earlier)
        resumed = await (await client.resume(job)).wait()
        assert resumed.state == "succeeded"
        with pytest.raises(ImportRefusedError, match="--replace"):
            await client.import_job(later, update=True)
        assert await client.import_job(later, replace=True) == job
        assert (await client.jobs.status(job)).state == "succeeded"
        assert await client.jobs.result(job) == Array.scalar(2, int)

        exported = tmp_path / "exported.car"
        await client.export_job(job, exported)
        assert read_record(exported).root == read_record(later).root

        # The attempt this copy made is no attempt of the job, and the next update does not
        # count it as something the record lacks.
        assert await client.import_job(later, update=True) == job

        # The step fails if it runs, so a success here came from the step cache.
        FAIL[0] = True
        try:
            again = await client.run(once_flaky, {"x": Array.scalar(1, int)})
        finally:
            FAIL[0] = False
        assert again.job_id != job and again.state == "succeeded"
        assert await client.jobs.result(again.job_id) == Array.scalar(2, int)


async def test_replace_keeps_a_cache_key_usable_after_a_step_that_failed_here(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """A copy resumed here that failed again leaves a cache key pointing at the failed attempt.
    Replacing the copy removes that attempt, and a later job runs the step."""
    job, earlier, later = await stopped_and_finished(database, tmp_path)
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(earlier)
        FAIL[0] = True
        try:
            assert (await (await client.resume(job)).wait()).state == "stopped"
        finally:
            FAIL[0] = False
        assert await client.import_job(later, replace=True) == job
        assert (await client.jobs.status(job)).state == "succeeded"
        again = await client.run(once_flaky, {"x": Array.scalar(1, int)})
        assert again.state == "succeeded"


async def test_replace_refuses_a_job_a_process_holds(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    job, earlier, later = await stopped_and_finished(database, tmp_path)
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(earlier)
        await a_runner(elsewhere, job, flaky, once_flaky)  # takes the job, runs nothing
        with pytest.raises(ImportRefusedError, match="held by a process"):
            await client.import_job(later, replace=True)


async def test_update_refuses_a_row_the_record_lacks_or_contradicts(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    job, earlier, later = await stopped_and_finished(database, tmp_path)
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(earlier)
        root = await client.jobs.describe(job)
        root_id = root.invocations["root"].invocation.id
        identity = root.invocations["root"].nodes[0].identity.cid
        # A name the record does not have.
        async with elsewhere.transaction() as tx:
            await tx.execute(
                insert(node_names).values(invocation_id=root_id, name="extra", identity=identity)
            )
        with pytest.raises(ImportRefusedError, match="node_names .* is not in the record"):
            await client.import_job(later, update=True)
        async with elsewhere.transaction() as tx:
            await tx.execute(delete(node_names).where(node_names.c.name == "extra"))
            # An invocation column the record has otherwise.
            await tx.execute(
                update(workflow_invocations)
                .where(workflow_invocations.c.id == root_id)
                .values(cache_key_prefix="elsewhere")
            )
        with pytest.raises(ImportRefusedError, match="workflow_invocations .* differs"):
            await client.import_job(later, update=True)
        assert (await client.jobs.status(job)).state == "stopped"  # nothing was written


@pytest.mark.parametrize("without_history", [False, True])
async def test_a_resumed_import_syncs_history_back_to_its_origin(
    database: Database, elsewhere: Database, tmp_path: Path, without_history: bool
) -> None:
    first = tmp_path / "first.car"
    finished = tmp_path / "finished.car"
    FAIL[0] = True
    try:
        async with a_client(database, flaky, once_flaky) as client:
            job = (await client.run(once_flaky, {"x": Array.scalar(1, int)})).job_id
            await client.export_job(job, first)
    finally:
        FAIL[0] = False
    if without_history:
        file = read_record(first)
        data = DEFAULT_CONVERTER.unstructure_entity(file.record)
        assert isinstance(data, dict)
        data["$type"] = "org.transluce.fxtr.project.JobRecord.v0.1"
        del data["execution"]
        encoded = codec.encode(data)
        root = codec.compute_cid(encoded)
        write_car(first, [root], [(root, encoded), *file.blocks.items()])
        assert read_record(first).record.execution is None
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(first)
        assert (await (await client.resume(job)).wait()).state == "succeeded"
        root = await client.export_job(job, finished)
    async with a_client(database, flaky, once_flaky) as client:
        await client.import_job(finished, update=True)
        await client.import_job(finished, update=True)
        assert (await client.jobs.status(job)).state == "succeeded"
        complete = tmp_path / "complete.car"
        complete_root = await client.export_job(job, complete)
        if not without_history:
            assert complete_root == root
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(complete, update=True)
        assert await client.export_job(job, tmp_path / "again.car") == complete_root
    history = read_record(complete).record.execution
    assert history is not None
    assert len(history.job_attempts) == 2
    assert [line.message for line in history.logs] == ["flaky attempt", "flaky attempt"]


async def test_sync_refuses_changed_or_missing_execution_history(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    first = tmp_path / "first.car"
    FAIL[0] = True
    try:
        async with a_client(database, flaky, once_flaky) as client:
            job = (await client.run(once_flaky, {"x": Array.scalar(1, int)})).job_id
            await client.export_job(job, first)
    finally:
        FAIL[0] = False
    file = read_record(first)
    history = file.record.execution
    assert history is not None and history.logs
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(first)
        for logs in ((), (dataclasses.replace(history.logs[0], message="changed"),)):
            bad = tmp_path / "altered.car"
            write_record(
                bad, with_history(file.record, dataclasses.replace(history, logs=logs)), file.blocks
            )
            with pytest.raises(ImportRefusedError, match="attempt_logs"):
                await client.import_job(bad, update=True)
        assert await client.export_job(job, tmp_path / "unchanged.car") == file.root


async def test_exports_of_a_settled_job_agree_after_its_job_attempt_heartbeats(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """The owning process refreshes heartbeat_at after the job attempt settles too (a draining
    soft cancel keeps heartbeating). The export writes the settling time in its place, so two
    exports of one settled job are the same record, and a push of each sees nothing new."""
    first, second = tmp_path / "first.car", tmp_path / "second.car"
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        assert (await client.export_job(job, first)) is not None
        async with database.transaction() as tx:
            (job_attempt,) = await execution.list_job_attempts(tx, job)
            assert job_attempt.state == "succeeded"
            assert await execution.heartbeat(tx, job_attempt.id, job_attempt.owner) == "succeeded"
            assert (await execution.get_job_attempt(tx, job_attempt.id)).heartbeat_at > (
                job_attempt.heartbeat_at
            )
        assert await client.export_job(job, second) == await client.export_job(job, first)
    (before,), (after,) = history_of(first)[2].job_attempts, history_of(second)[2].job_attempts
    assert before == after and after.heartbeat_at == after.settled_at
    async with a_client(elsewhere, double, doubled) as client:
        await client.import_job(first)
        assert await client.import_job(second, update=True) == job


def a_job_attempt_in_an_unknown_state(history: ExecutionRecord) -> ExecutionRecord:
    (job_attempt,) = history.job_attempts
    return dataclasses.replace(
        history, job_attempts=(dataclasses.replace(job_attempt, state="exploded"),)
    )


def an_attempt_in_an_unknown_state(history: ExecutionRecord) -> ExecutionRecord:
    first, *rest = history.attempts
    return dataclasses.replace(
        history, attempts=(dataclasses.replace(first, state="exploded"), *rest)
    )


def an_attempt_of_an_unknown_kind(history: ExecutionRecord) -> ExecutionRecord:
    first, *rest = history.attempts
    return dataclasses.replace(history, attempts=(dataclasses.replace(first, kind="thing"), *rest))


def a_workflow_attempt_at_a_cache_key(history: ExecutionRecord) -> ExecutionRecord:
    return dataclasses.replace(
        history,
        attempts=tuple(
            dataclasses.replace(a, cache_key="root/double") if a.kind == "workflow" else a
            for a in history.attempts
        ),
    )


def a_step_attempt_of_an_invocation(history: ExecutionRecord) -> ExecutionRecord:
    (invocation_id,) = {a.invocation_id for a in history.attempts if a.kind == "workflow"}
    return dataclasses.replace(
        history,
        attempts=tuple(
            dataclasses.replace(a, invocation_id=invocation_id) if a.kind == "step" else a
            for a in history.attempts
        ),
    )


def an_event_of_an_unknown_kind(history: ExecutionRecord) -> ExecutionRecord:
    first, *rest = history.events
    return dataclasses.replace(history, events=(dataclasses.replace(first, kind="jump"), *rest))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (a_job_attempt_in_an_unknown_state, "unknown state 'exploded'"),
        (an_attempt_in_an_unknown_state, "unknown state 'exploded'"),
        (an_attempt_of_an_unknown_kind, "unknown kind 'thing'"),
        (a_workflow_attempt_at_a_cache_key, "does not name its invocation alone"),
        (a_step_attempt_of_an_invocation, "does not name a cache key and a request"),
        (an_event_of_an_unknown_kind, "unknown kind 'jump'"),
    ],
)
async def test_history_the_tables_would_not_accept_is_refused_before_the_database_sees_it(
    database: Database,
    elsewhere: Database,
    tmp_path: Path,
    mutate: Callable[[ExecutionRecord], ExecutionRecord],
    message: str,
) -> None:
    """A value the tables' check constraints reject is a refusal of the record, not a database
    error out of the import."""
    path = tmp_path / "job.car"
    await run_and_export(database, path)
    record, blocks, history = history_of(path)
    assert {a.kind for a in history.attempts} == {"workflow", "step"} and history.events
    validate_execution_record(history)
    with pytest.raises(RecordFileError, match=message):
        validate_execution_record(mutate(history))
    write_record(tmp_path / "bad.car", with_history(record, mutate(history)), blocks)
    async with a_client(elsewhere, double, doubled) as client:
        with pytest.raises(ImportRefusedError, match=message):
            await client.import_job(tmp_path / "bad.car")
        assert await client.list_jobs() == []


def checked_values(table: Table, constraint: str) -> set[str]:
    """The values a table's ``column IN (...)`` check constraint lists."""
    (check,) = [
        c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == constraint
    ]
    return set(re.findall(r"'(\w+)'", str(check.sqltext)))


async def test_validation_accepts_exactly_the_values_the_tables_check(
    database: Database, tmp_path: Path
) -> None:
    """The states and kinds ``validate_execution_record`` accepts are the ones schema.py's check
    constraints list, less the live state of each (only settled job attempts and closed attempts
    travel), so neither can change without the other."""
    path = tmp_path / "job.car"
    await run_and_export(database, path)
    history = history_of(path)[2]

    def accepted(
        candidates: set[str], put: Callable[[ExecutionRecord, str], ExecutionRecord]
    ) -> set[str]:
        passing: set[str] = set()
        for value in candidates | {"other"}:
            try:
                validate_execution_record(put(history, value))
            except RecordFileError:
                continue
            passing.add(value)
        return passing

    def job_attempt_state(h: ExecutionRecord, value: str) -> ExecutionRecord:
        rows = tuple(dataclasses.replace(r, state=value) for r in h.job_attempts)
        return dataclasses.replace(h, job_attempts=rows)

    def attempt_state(h: ExecutionRecord, value: str) -> ExecutionRecord:
        rows = tuple(dataclasses.replace(r, state=value) for r in h.attempts)
        return dataclasses.replace(h, attempts=rows)

    def event_kind(h: ExecutionRecord, value: str) -> ExecutionRecord:
        rows = tuple(dataclasses.replace(r, kind=value) for r in h.events)
        return dataclasses.replace(h, events=rows)

    job_attempt_states = checked_values(job_attempts, "job_attempts_state")
    assert accepted(job_attempt_states, job_attempt_state) == job_attempt_states - {"active"}
    attempt_states = checked_values(attempts, "attempts_state")
    assert accepted(attempt_states, attempt_state) == attempt_states - {"running"}
    event_kinds = checked_values(attempt_events, "attempt_events_kind")
    assert accepted(event_kinds, event_kind) == event_kinds
    # The record carries an attempt of each kind the table accepts, and validates as exported.
    assert {a.kind for a in history.attempts} == checked_values(attempts, "attempts_kind")


async def test_history_naming_a_fact_the_database_lacks_is_refused(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """History naming a job attempt with no fact, or an attempt of an invocation or a step
    request the database lacks, is refused as a record's fault, naming the fact the tables'
    foreign keys miss."""
    path = tmp_path / "job.car"
    await run_and_export(database, path)
    history = history_of(path)[2]
    (job_attempt,) = history.job_attempts
    workflow_attempt = next(a for a in history.attempts if a.kind == "workflow")
    step_attempt = next(a for a in history.attempts if a.kind == "step")
    stray = str(uuid.uuid4())
    of_a_stray_job_attempt = dataclasses.replace(
        history, job_attempts=(job_attempt, dataclasses.replace(job_attempt, id=stray))
    )
    of_a_stray_invocation = dataclasses.replace(
        history,
        attempts=(
            *history.attempts,
            dataclasses.replace(workflow_attempt, id=str(uuid.uuid4()), invocation_id=stray),
        ),
    )
    of_a_stray_request = dataclasses.replace(
        history,
        attempts=(
            *history.attempts,
            dataclasses.replace(step_attempt, id=str(uuid.uuid4()), request=b"\x03" * 36),
        ),
    )
    async with a_client(elsewhere, double, doubled) as client:
        await client.import_job(path)
    for altered, message in (
        (of_a_stray_job_attempt, f'{stray}.*not present in table "job_attempt_provenance"'),
        (of_a_stray_invocation, f'\\({stray}\\) is not present in table "workflow_invocations"'),
        (of_a_stray_request, 'not present in table "step_requests"'),
    ):
        with pytest.raises(RecordFileError, match=message):
            async with elsewhere.transaction() as tx:
                await insert_execution_record(tx, altered)


async def test_recording_a_jobs_entities_again_gives_what_its_import_recorded(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """``record_job_entities`` fills ``job_entities`` for a job the table does not name (one
    imported before the table existed), with exactly what importing its record records."""
    from fxtr.runner.postgres.entities import PostgresEntityStorage

    job, _, later = await stopped_and_finished(database, tmp_path)
    reached = set(read_record(later).blocks)
    async with a_client(elsewhere, flaky, once_flaky) as client:
        await client.import_job(later)
    async with elsewhere.transaction() as tx:
        await tx.execute(delete(job_entities).where(job_entities.c.job_id == job))
    assert await entities_of(elsewhere, job) == set()
    async with elsewhere.transaction() as tx:
        recorded = await job_records.record_job_entities(tx, PostgresEntityStorage(elsewhere), job)
    assert recorded == len(reached)
    assert await entities_of(elsewhere, job) == reached
    async with elsewhere.transaction() as tx:
        # Recording again changes nothing.
        assert (
            await job_records.record_job_entities(tx, PostgresEntityStorage(elsewhere), job)
            == recorded
        )
    assert await entities_of(elsewhere, job) == reached


async def test_any_of_takes_more_ids_than_a_statement_takes_parameters(database: Database) -> None:
    """A job of 16,384 steps has more ids than Postgres takes as a statement's parameters."""
    ids = [uuid.uuid4() for _ in range(40_000)]
    async with database.transaction() as tx:
        found = await tx.execute(select(jobs.c.id).where(any_of(jobs.c.id, ids)))
        assert found.all() == []
