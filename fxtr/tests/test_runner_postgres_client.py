"""The local project client: entities and arrays through it, and jobs submitted, watched,
cancelled, interrupted, and resumed, all against a fake environment that pins a fixture code
version."""

from __future__ import annotations

import contextlib
import inspect
import sys
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from postgres_testing import (
    FAST,
    TEST_CODE_VERSION,
    FakeEnvironment,
    a_client,
    a_project,
    a_runner,
    wait_until,
)

from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.schema import IdSchema
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.registry import FunctionRegistry, RegistryError
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.project.client import (
    JobNotFinishedError,
    JobNotFoundError,
    ProjectClientError,
)
from fxtr.project.code_version import RepoSource
from fxtr.project.config import write_local_config
from fxtr.project.environment import Problem

pytest.importorskip("sqlalchemy")

from sqlalchemy import event  # noqa: E402

from fxtr.runner.postgres.client import (  # noqa: E402
    EnvironmentNotSyncedError,
    LocalJobHandle,
    LocalProjectClient,
)
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.runner import JobEvictedError  # noqa: E402

pytestmark = pytest.mark.anyio

NODE_TYPE = "test.fxtr.node"
IDS_TYPE = ArrayType.of(("[id: int]", int))
IDS = Array.from_items([((1,), 10), ((2,), 20)], IDS_TYPE)

GATE: anyio.Event | None = None
RAISE = [False]


@pytest.fixture(autouse=True)
def reset_knobs() -> None:
    global GATE
    GATE = None
    RAISE[0] = False


async def gate_open() -> None:
    if GATE is not None:
        await GATE.wait()


# ---- the functions the tests run ----


@workflow(inputs={"x": int}, output=int)
async def echo(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return x


@workflow(inputs={"x": int}, output=int)
async def gated(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    await x.observe()
    await gate_open()
    return x


@workflow(inputs={"x": int}, output=int)
async def raising(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    if RAISE[0]:
        raise RuntimeError("boom")
    return x


@step(inputs={"x": int}, output=int)
async def double(context: StepContext, x: int) -> int:
    await gate_open()
    return 2 * x


@workflow(inputs={"ids": IDS_TYPE}, output=IDS_TYPE)
async def double_each(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("double", double, {"x": ids}, map_over=["id"])


@step(inputs={"x": int}, output=int)
async def triple(context: StepContext, x: int) -> int:
    return 3 * x


@workflow(inputs={"ids": IDS_TYPE}, output=IDS_TYPE)
async def triple_each(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("triple", triple, {"x": ids}, map_over=["id"])


def scalar(value: int) -> dict[str, Array[Any]]:
    return {"x": Array.scalar(value, int)}


# ---- entities and arrays ----


async def test_the_client_is_the_store_and_holds_the_arrays(database: Database) -> None:
    bio_type = ("[person: str]", IdSchema(NODE_TYPE))
    async with a_client(database) as client:
        info = await client.create_array("bios", bio_type)
        assert info.name == "bios" and info.row_count == 0
        ada = await client.store_raw_data({"$type": NODE_TYPE, "name": "Ada"})
        assert client.entities.pending_count == 1  # pending until something references it
        info = await client.upsert_rows("bios", Array.from_items([(("ada",), ada)], bio_type))
        assert client.entities.pending_count == 0 and info.row_count == 1
        snap = await client.snapshot_array("bios")
        assert await client.load(snap.array) == await client.load_array("bios")
        assert [i.name for i in await client.list_arrays()] == ["bios"]
        assert [s.array for s in await client.list_snapshots("bios")] == [snap.array]
        assert await client.delete_rows("bios", [("ada",)]) == 1
        assert await client.clear_array("bios") == 0
        with pytest.raises(ProjectClientError):
            await client.create_array("bios", ("[other: int]", int))  # another type
        with pytest.raises(KeyError):
            await client.describe_array("nope")
        await client.drop_array("bios")
        assert await client.list_arrays() == []


# ---- submit ----


async def test_submit_runs_the_job_here_and_records_the_pin(database: Database) -> None:
    environment = FakeEnvironment()
    async with a_client(database, echo, environment=environment) as client:
        handle = await client.submit(echo, scalar(7))
        assert isinstance(handle, LocalJobHandle)
        status = await handle.wait()
        assert status.state == "succeeded" and status.reason is None and not status.failed
        assert await handle.result() == Array.scalar(7, int)
        assert (await handle.status()).result is not None
        assert handle.error is None
        # The job attempt records the code this process ran: the pin, verified, and where each
        # function the client's registry runs is (the registry holds echo alone).
        (job_attempt,) = (await client.jobs.status(handle.job_id)).job_attempts
        code_version = await client.load(job_attempt.code_version)
        assert (code_version.commit, code_version.tree) == (
            TEST_CODE_VERSION.commit,
            TEST_CODE_VERSION.tree,
        )
        assert code_version.project_path == "." and code_version.environment_verified is True
        (source,) = code_version.functions.values()
        assert code_version.functions.keys() == {echo.name}
        assert isinstance(source, RepoSource) and source.symbol == echo.body.__qualname__
        assert source.file.endswith("tests/test_runner_postgres_client.py")
        lines, start = inspect.getsourcelines(echo.body)
        assert (source.start_line, source.end_line) == (start, start + len(lines) - 1)
        assert environment.pins == [False] and environment.checks == 1
        # Listed, newest first, with its state.
        (summary,) = await client.list_jobs()
        assert summary.job_id == handle.job_id and summary.state == "succeeded"
        assert summary.workflow_name == echo.name
        # run is submit then wait; a name works as well as the workflow.
        assert (await client.run(echo.name, scalar(8))).state == "succeeded"
        assert [s.state for s in await client.list_jobs()] == ["succeeded", "succeeded"]


async def test_submit_requires_the_registered_workflow(database: Database) -> None:
    async with a_client(database, echo) as client:
        with pytest.raises(RegistryError, match="No function named"):
            await client.submit(gated, scalar(1))
        with pytest.raises(RegistryError, match="not the one given"):
            other = workflow(inputs={"x": int}, output=int, name=echo.name)(echo.body)
            await client.submit(other, scalar(1))
        assert await client.list_jobs() == []  # nothing was written


async def test_the_environment_check_refuses_before_anything_is_written(
    database: Database,
) -> None:
    environment = FakeEnvironment(problems=[Problem("the lockfile is stale", "uv lock")])
    async with a_client(database, echo, environment=environment) as client:
        with pytest.raises(EnvironmentNotSyncedError) as info:
            await client.submit(echo, scalar(1))
        assert "the lockfile is stale (fix: uv lock)" in str(info.value)
        assert "verify_environment=False" in str(info.value)
        assert info.value.problems == (environment.problems[0],)
        assert environment.pins == [] and await client.list_jobs() == []
        # Skipping the check is recorded; the pin still happens, with allow_staged as given.
        handle = await client.submit(echo, scalar(1), verify_environment=False, allow_staged=True)
        assert (await handle.wait()).state == "succeeded"
        (job_attempt,) = (await client.jobs.status(handle.job_id)).job_attempts
        assert (await client.load(job_attempt.code_version)).environment_verified is False
        assert environment.pins == [True] and environment.checks == 1


async def test_a_failed_job_reports_its_errors(database: Database) -> None:
    RAISE[0] = True
    async with a_client(database, raising) as client:
        status = await client.run(raising, scalar(1))
        assert status.state == "stopped" and status.failed and status.reason is None
        assert status.errors["root"].type_name == "RuntimeError"
        handle = await client.job(status.job_id)
        with pytest.raises(JobNotFinishedError):
            await handle.result()


async def test_the_client_must_be_entered_to_submit(database: Database) -> None:
    client = a_client(database, echo)
    with pytest.raises(RuntimeError, match="enter it first"):
        await client.submit(echo, scalar(1))


# ---- resume ----


async def test_resume_continues_a_failed_job_at_this_process_pin(database: Database) -> None:
    RAISE[0] = True
    async with a_client(database, raising) as client:
        failed = await client.run(raising, scalar(5))
        assert failed.failed
        RAISE[0] = False
        handle = await client.resume(failed.job_id)
        assert (await handle.wait()).state == "succeeded"
        assert await handle.result() == Array.scalar(5, int)
        job_attempts = (await client.jobs.status(handle.job_id)).job_attempts
        assert [ja.seq for ja in job_attempts] == [1, 2]
        assert len({ja.owner for ja in job_attempts}) == 2  # one owner string per run
        with pytest.raises(ProjectClientError, match="has succeeded"):
            await client.resume(handle.job_id)
        with pytest.raises(JobNotFoundError):
            await client.resume(uuid.uuid4())  # type: ignore[arg-type]
        with pytest.raises(JobNotFoundError):
            await client.job(uuid.uuid4())  # type: ignore[arg-type]


async def test_resume_is_refused_while_the_job_runs_here(database: Database) -> None:
    global GATE
    GATE = anyio.Event()
    async with a_client(database, gated) as client:
        handle = await client.submit(gated, scalar(1))
        await wait_until_running(client, handle)
        with pytest.raises(ProjectClientError, match="is being run by"):
            await client.resume(handle.job_id)
        GATE.set()
        assert (await handle.wait()).state == "succeeded"


async def test_resume_takes_over_a_stale_lease(database: Database) -> None:
    """A job whose process died mid-episode is stale after the project's threshold; a resume
    here evicts it and finishes the job."""
    global GATE
    GATE = anyio.Event()
    async with a_client(database, gated) as client:
        # Another process: a runner whose heartbeat stops (its task is killed) while its body runs.
        job = await client.jobs.create(gated, scalar(3))
        runner = await a_runner(database, job, gated)
        handle = await client.job(job)
        async with anyio.create_task_group() as tg:
            tg.start_soon(runner.run)
            await wait_until_running(client, handle)
            tg.cancel_scope.cancel()  # the process dies; the runner's teardown settles its episode
        # The teardown settled it cancelled, so it is not stale but stopped; make it look like a
        # crash instead: active, with a heartbeat that stopped. Each heartbeat is far from the
        # threshold, so how long the steps between take cannot change the outcome.
        await set_active_with_heartbeat(database, job, timedelta(hours=1))
        assert (await handle.status()).state == "running"
        with pytest.raises(ProjectClientError, match="is being run by"):
            await client.resume(handle.job_id)  # fresh: refused
        stale_after = a_project(database).stale_after
        await set_active_with_heartbeat(database, job, -(stale_after + timedelta(seconds=1)))
        GATE.set()
        resumed = await client.resume(job)
        assert (await resumed.wait()).state == "succeeded"
        job_attempts = (await client.jobs.status(job)).job_attempts
        assert job_attempts[0].evicted and "taken over" in (job_attempts[0].evicted_reason or "")


async def set_active_with_heartbeat(database: Database, job: Any, offset: timedelta) -> None:
    """Make the job's job attempts active, with their last heartbeat ``offset`` from the
    database's now: ahead of it to read as fresh, more than the staleness threshold behind it to
    read as stale."""
    from sqlalchemy import func, update

    from fxtr.runner.postgres.schema import job_attempts

    async with database.transaction() as tx:
        await tx.execute(
            update(job_attempts)
            .where(job_attempts.c.job_id == job)
            .values(state="active", settled_at=None, heartbeat_at=func.now() + offset)
        )


async def wait_until_running(client: LocalProjectClient, handle: LocalJobHandle) -> None:
    """Wait until the job's root body is running, not merely due: a job whose root is queued
    under its active job attempt reports ``running`` too, and an interrupt or cancel then stops
    it before any body has started."""

    async def root_runs() -> bool:
        report = await client.jobs.status(handle.job_id)
        return report.invocations[report.job.root].state == "running"

    await wait_until(root_runs)


# ---- wait, cancel, interrupt ----


async def test_wait_follows_a_job_this_client_did_not_start(database: Database) -> None:
    """A job another runner has taken is running; ``wait`` on this client's handle follows it until
    it finishes. A job nobody has taken is stopped, so waiting on it returns at once."""
    async with a_client(database, echo) as client:
        untaken = await client.jobs.create(echo, scalar(1))
        assert (await (await client.job(untaken)).wait()).state == "stopped"
        job = await client.jobs.create(echo, scalar(2))
        runner = await a_runner(database, job, echo)
        handle = await client.job(job)
        assert (await handle.status()).state == "running"
        outcome: list[Any] = []

        async def wait() -> None:
            outcome.append(await handle.wait())

        async with anyio.create_task_group() as tg:
            tg.start_soon(wait)
            await anyio.sleep(0.1)
            assert outcome == []  # still running: taken, but its runner has not run it
            await runner.run()
        assert outcome[0].state == "succeeded" and handle.error is None


async def test_a_hard_cancel_evicts_the_run_and_the_job_can_be_resumed(
    database: Database,
) -> None:
    global GATE
    GATE = anyio.Event()
    async with a_client(database, gated) as client:
        handle = await client.submit(gated, scalar(4))
        await wait_until_running(client, handle)
        await handle.cancel(hard=True)
        status = await handle.wait()
        assert status.state == "stopped" and not status.failed
        assert status.reason == "cancelled"
        assert isinstance(handle.error, JobEvictedError)
        await handle.cancel()  # nothing running: a no-op
        GATE.set()
        resumed = await client.resume(handle.job_id)
        assert (await resumed.wait()).state == "succeeded" and resumed.error is None


async def test_a_soft_cancel_lets_running_steps_finish_into_the_cache(
    database: Database,
) -> None:
    global GATE
    GATE = anyio.Event()
    async with a_client(database, double_each, double) as client:
        handle = await client.submit(double_each, {"ids": IDS})
        await wait_until(lambda: step_is_running(client))
        await handle.cancel()
        GATE.set()
        status = await handle.wait()
        # The steps completed (their results are cached), but the job took nothing more: stopped.
        assert status.state == "stopped" and status.reason == "cancelled"
        entries = await client.list_cache_entries()
        assert {(e.step_name, e.state) for e in entries} == {(double.name, "completed")}
        resumed = await client.resume(handle.job_id)
        assert (await resumed.wait()).state == "succeeded"
        assert await resumed.result() == Array.from_items([((1,), 20), ((2,), 40)], IDS_TYPE)


async def step_is_running(client: LocalProjectClient) -> bool:
    return any(e.state == "running" for e in await client.list_cache_entries())


async def test_interrupts_stop_gently_then_hard_then_leave(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    global GATE
    GATE = anyio.Event()
    with pytest.raises(KeyboardInterrupt):
        async with a_client(database, gated) as client:
            handle = await client.submit(gated, scalar(1))
            await wait_until_running(client, handle)
            client.interrupt()  # gentle: the stuck body keeps running
            await anyio.sleep(FAST.reconcile_interval)
            assert (await handle.status()).state == "running"
            with pytest.raises(ProjectClientError, match="accepts no new jobs"):
                await client.submit(gated, scalar(2))
            client.interrupt()  # the running work is abandoned
            status = await handle.wait()
            assert status.state == "stopped" and status.reason == "interrupted"
            client.interrupt()  # nothing running: the block is cancelled
            await anyio.sleep(10)
            pytest.fail("the block should have been cancelled")
    err = capsys.readouterr().err
    assert "stopping 1 job gently" in err and "Press Ctrl-C again" in err
    assert "stopping the running work of 1 job now" in err
    assert "cancelling all async tasks" in err


async def test_an_interrupt_with_nothing_running_leaves_at_once(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(KeyboardInterrupt):
        async with a_client(database) as client:
            client.interrupt()
            await anyio.sleep(10)
    assert "cancelling all async tasks" in capsys.readouterr().err


async def test_leaving_the_block_with_an_error_stops_the_jobs_resumably(
    database: Database,
) -> None:
    global GATE
    GATE = anyio.Event()
    job: Any = None
    with pytest.raises(ValueError, match="the launcher failed") as info:
        async with a_client(database, gated) as client:
            handle = await client.submit(gated, scalar(6))
            job = handle.job_id
            await wait_until_running(client, handle)
            try:
                raise KeyError("the underlying problem")
            except KeyError as exc:
                raise ValueError("the launcher failed") from exc
    # The block's exception comes back as itself, with its own cause and context intact.
    assert isinstance(info.value.__cause__, KeyError)
    assert isinstance(info.value.__context__, KeyError)
    GATE.set()
    async with a_client(database, gated) as client:
        handle = await client.job(job)
        assert (await handle.status()).state == "stopped"
        assert (await (await client.resume(job)).wait()).state == "succeeded"


async def test_leaving_the_block_waits_for_the_jobs(database: Database) -> None:
    global GATE
    GATE = anyio.Event()
    async with a_client(database, gated) as client:
        handle = await client.submit(gated, scalar(1))
        await wait_until_running(client, handle)

        async def open_later() -> None:
            await anyio.sleep(0.2)
            assert GATE is not None
            GATE.set()

        client._task_group().start_soon(open_later)
    # The block waited for the job, which the gate let finish.
    assert (await handle.status()).state == "succeeded"


# ---- the step cache ----


async def test_cache_entries_are_listed_and_cleared_by_selection(database: Database) -> None:
    async with a_client(database, double_each, double, triple_each, triple) as client:
        assert (await client.run(double_each, {"ids": IDS})).state == "succeeded"
        assert (await client.run(triple_each, {"ids": IDS}, root="other")).state == "succeeded"
        keys = {e.cache_address for e in await client.list_cache_entries()}
        assert keys == {
            "root/double[id:1]",
            "root/double[id:2]",
            "other/triple[id:1]",
            "other/triple[id:2]",
        }
        assert {e.cache_address for e in await client.list_cache_entries("root")} == {
            "root/double[id:1]",
            "root/double[id:2]",
        }
        assert {e.cache_address for e in await client.list_cache_entries("root/double")} == {
            "root/double[id:1]",
            "root/double[id:2]",
        }
        assert await client.list_cache_entries("root/doub") == []  # a whole component or nothing
        assert await client.list_cache_entries("r__t") == []  # LIKE wildcards are text
        assert await client.list_cache_entries("%") == []
        # A mapped value can be a wildcard.
        assert {e.cache_address for e in await client.list_cache_entries("root/double[id:*]")} == {
            "root/double[id:1]",
            "root/double[id:2]",
        }
        assert [e.cache_address for e in await client.list_cache_entries("other/triple[id:2]")] == [
            "other/triple[id:2]"
        ]
        with pytest.raises(ValueError, match="a whole mapped value"):
            await client.list_cache_entries("root/*")
        assert {e.cache_address for e in await client.list_cache_entries(step=triple.name)} == {
            "other/triple[id:1]",
            "other/triple[id:2]",
        }
        assert len(await client.list_cache_entries(state="completed")) == 4
        assert await client.list_cache_entries(state="failed") == []
        with pytest.raises(ValueError):
            await client.clear_cache_entries()
        with pytest.raises(ValueError):
            await client.clear_cache_entries("root", addresses=["root/double[id:1]"])
        assert await client.clear_cache_entries(addresses=["root/double[id:1]", "nope"]) == 1
        assert await client.clear_cache_entries("other", step=double.name) == 0
        assert await client.clear_cache_entries("other/triple[id:*]") == 2
        assert await client.clear_cache_entries("") == 1
        assert await client.list_cache_entries() == []
        # The jobs keep their bound results.
        (job,) = [s for s in await client.list_jobs() if s.workflow_name == double_each.name]
        assert await (await client.job(job.job_id)).result() == Array.from_items(
            [((1,), 20), ((2,), 40)], IDS_TYPE
        )


@step(inputs={"x": int}, output=int)
async def square(context: StepContext, x: int) -> int:
    context.log(f"squaring {x}")
    return x * x


@workflow(inputs={"ids": IDS_TYPE}, output=IDS_TYPE)
async def square_each(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    # One prefix for every root, so a job under another root takes the results from the cache.
    return context.run_step(
        "square", square, {"x": ids}, map_over=["id"], override_cache_address_prefix="shared/sq"
    )


def squares(n: int) -> tuple[Array[int], Array[int]]:
    return (
        Array.from_items([((i,), i) for i in range(n)], IDS_TYPE),
        Array.from_items([((i,), i * i) for i in range(n)], IDS_TYPE),
    )


@contextlib.contextmanager
def statements_on(database: Database, *tables: str) -> Iterator[list[int]]:
    """Count the statements that name any of ``tables`` while the block runs."""
    counted = [0]

    def count(*args: Any) -> None:
        statement: str = args[2]
        counted[0] += any(table in statement for table in tables)

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        yield counted
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def test_taking_and_describing_many_cached_steps_costs_a_few_statements(
    database: Database,
) -> None:
    # Resolving, taking and describing the steps of a mapped run node reads and writes each kind
    # of row for all its keys at once: a job that finds 60 results in the cache needs far fewer
    # statements about its requests and bindings than it has keys.
    ids, expected = squares(60)
    async with a_client(database, square_each, square) as client:
        assert (await client.run(square_each, {"ids": ids}, root="first")).state == "succeeded"
        tables = ("job_step_requests", "job_step_bindings", "step_cache", "attempt_logs")
        with statements_on(database, *tables) as replayed:
            handle = await client.submit(square_each, {"ids": ids}, root="second")
            assert (await handle.wait()).state == "succeeded"
        assert await handle.result() == expected
        assert replayed[0] < 30
        with statements_on(database, *tables) as described:
            detail = await client.jobs.describe_invocation(handle.job_id, "second")
        assert detail is not None
        assert set(detail.step_states.values()) == {"completed"}
        assert all(result is not None for result in detail.step_results.values())
        assert described[0] < 10


async def test_many_key_reads_and_writes_split_their_keys_into_batches(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fxtr.runner.postgres import entities, ledger, logs

    monkeypatch.setattr(ledger, "BATCH_SIZE", 2)
    monkeypatch.setattr(entities, "_IDS_PER_QUERY", 2)
    monkeypatch.setattr(logs, "_ATTEMPTS_PER_QUERY", 2)
    ids, expected = squares(5)
    async with a_client(database, square_each, square) as client:
        first = await client.submit(square_each, {"ids": ids}, root="first")
        assert (await first.wait()).state == "succeeded"
        second = await client.submit(square_each, {"ids": ids}, root="second")
        assert (await second.wait()).state == "succeeded"
        assert await first.result() == expected and await second.result() == expected
        detail = await client.jobs.describe_invocation(first.job_id, "first")
        assert detail is not None and len(detail.step_results) == 5
        assert set(detail.step_states.values()) == {"completed"}
        steps = [await client.jobs.describe_step(first.job_id, key) for key in detail.step_results]
        assert sorted(s.description.logs[0].message for s in steps if s is not None) == [
            f"squaring {i}" for i in range(5)
        ]


# ---- from_project ----


async def test_from_project_locates_loads_env_imports_connects_and_creates_the_schema(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "proj"\n[tool.fxtr]\nmodules = ["proj_steps"]\nstale_after = 2\n'
    )
    (root / ".env").write_text("FXTR_TEST_FROM_ENV_FILE=loaded\n")
    monkeypatch.setenv("FXTR_TEST_FROM_ENV_FILE", "")
    monkeypatch.delenv("FXTR_TEST_FROM_ENV_FILE")
    (root / "src" / "proj_steps.py").write_text(
        "import os\n"
        "SEEN = os.environ.get('FXTR_TEST_FROM_ENV_FILE')\n"
        "from fxtr.experiment.workflows import workflow\n"
        "@workflow(inputs={'x': int}, output=int, name='proj.echo')\n"
        "async def echo(context, x):\n    return x\n"
    )
    schema = f"test_{uuid.uuid4().hex}"
    write_local_config(root, database_url=database.url, schema=schema)
    monkeypatch.syspath_prepend(str(root / "src"))
    monkeypatch.delitem(sys.modules, "proj_steps", raising=False)
    registry = FunctionRegistry()
    client = LocalProjectClient.from_project(
        root / "src" / "launch.py",
        environment=FakeEnvironment(),
        registry=registry,
        settings=FAST,
        handle_sigint=False,
    )
    try:
        assert client.project.root == root.resolve() and client.project.schema == schema
        assert client.project.stale_after.total_seconds() == 2
        assert "proj_steps" in sys.modules  # imported: its workflow is registered globally
        assert sys.modules["proj_steps"].SEEN == "loaded"  # .env was loaded before the import
        from fxtr.experiment.registry import GLOBAL_REGISTRY

        registry.add(GLOBAL_REGISTRY.workflow("proj.echo"))
        async with client:
            status = await client.run("proj.echo", scalar(1))  # the schema was created on entry
            assert status.state == "succeeded"
    finally:
        await Database(database.url, pg_schema=schema).drop_schema()
