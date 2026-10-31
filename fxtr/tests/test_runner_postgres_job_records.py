"""Tests for job records: building a job's record from the ledger, exporting it as a CAR, and
importing it into another project: what travels, what is refused, and resuming an imported job."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from postgres_testing import TEST_CODE_VERSION, FakeEnvironment, a_client, a_runner
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert

from fxtr.core import codec
from fxtr.core.car import read_car, write_car
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
from fxtr.project.job_record import JOB_RECORD_TYPE, JobRecord

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.schema import (  # noqa: E402
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
        with pytest.raises(JobRunningError, match="held by a process"):
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
        assert status.state == "succeeded" and status.job_attempts == []
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
        assert step.attempt is None and step.result_provenance is not None
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
        # The failure did not travel: the job is merely stopped, with its history as facts.
        status = await client.jobs.status(job)
        assert status.state == "stopped" and not status.failed
        handle = await client.resume(job)
        assert (await handle.wait()).state == "succeeded"
        assert await client.jobs.result(job) == Array.scalar(2, int)
        # The new job attempt is numbered after the imported one.
        (taken,) = (await client.jobs.status(job)).job_attempts
        assert taken.seq == 2


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
