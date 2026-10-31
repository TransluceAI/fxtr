"""Jobs run end to end on the Postgres runner: a job runner takes a job's lease, runs every body
in process, and leaves the ledger and the execution state behind.

Each test starts a job with :meth:`~fxtr.runner.postgres.jobs.Jobs.start` and runs it with
:func:`run_job` (a :class:`~fxtr.runner.postgres.runner.JobRunner` to settlement) or with
:func:`~postgres_testing.runner_task` when it wants to act on the job from a second task: cancel
it, evict it, or run a second job against the same step cache key. Covered here: the happy path
and what the record looks like, failures of every kind, the verification run, stopping a
runner abruptly and resuming from the ledger, cancelling (soft, hard, and the two levels of
Ctrl-C), steps shared across jobs and the race rule over the cache entry, the limits, and
``ctx.log``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

import anyio
import pytest
from postgres_testing import (
    FAST,
    TEST_CODE_VERSION,
    a_client,
    a_code_version,
    a_runner,
    behind,
    produced_by,
    runner_task,
    wait_until,
)

from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.query_plan import PlannedQuery
from fxtr.entity_defns.schema import struct_of
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.project.client import CacheConflict, JobNotFoundError
from fxtr.project.formatting import conflict_patterns, format_cache_conflicts
from fxtr.project.running import report_job_status
from fxtr.runner import nodes
from fxtr.runner.query_codec import encode_query

pytest.importorskip("sqlalchemy")

from sqlalchemy import event  # noqa: E402

from fxtr.runner.postgres import execution, ledger  # noqa: E402
from fxtr.runner.postgres.client import LocalProjectClient  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.execution import (  # noqa: E402
    AttemptRecord,
    NotCancellableError,
    NotResumableError,
    StaleAttemptError,
)
from fxtr.runner.postgres.jobs import JobReport  # noqa: E402
from fxtr.runner.postgres.ledger import JobID  # noqa: E402
from fxtr.runner.postgres.runner import (  # noqa: E402
    JobEvictedError,
    RunLimits,
    RunnerSettings,
)
from fxtr.runner.postgres.schema import attempts, job_attempts  # noqa: E402

pytestmark = pytest.mark.anyio

ROWS_TYPE = ArrayType.of(("[id: int]", struct_of(x=int, y=str)))
ROWS = Array.from_records([{"id": 1, "x": 10, "y": "p"}, {"id": 2, "x": 20, "y": "q"}], ROWS_TYPE)


def one_step_over_rows(
    root: Any, build: Callable[[PlannedQuery[int]], PlannedQuery[int]]
) -> nodes.NodeIdentity:
    """The identity of the query node a workflow records for one step over its ``rows`` input."""
    (rows,) = [n.identity for n in root.nodes if isinstance(n.node, nodes.Input)]
    plan = build(PlannedQuery.over(0, ROWS_TYPE)).plan
    return nodes.Query(inputs=(rows,), plan=encode_query(plan)).content_id()


# Knobs the test bodies read, so one workflow can behave differently across attempts (a body
# that blocks until released, or that has been "fixed" between attempts).
GATE: anyio.Event | None = None
SECOND_GATE: anyio.Event | None = None
SOURCE_VALUE = [1]
EMIT_EXTRA = [True]
RAISE = [False]
BODY_CALLS = [0]  # how many times a counting body has been entered, across all its runs
ON_SECOND_RUN = ["nothing"]
ROLLS: list[str] = []
BODIES_RUNNING = [0]
BODIES_AT_ONCE = [0]
STEPS_RUNNING = [0]
STEPS_AT_ONCE = [0]


@pytest.fixture
def client(database: Database) -> LocalProjectClient:
    """A client for its inspection surface (``jobs``) and database; never entered, since these
    tests run jobs through runners of their own."""
    return a_client(database)


@pytest.fixture(autouse=True)
def reset_knobs() -> None:
    global GATE, SECOND_GATE
    GATE = None
    SECOND_GATE = None
    SOURCE_VALUE[0] = 1
    EMIT_EXTRA[0] = True
    RAISE[0] = False
    BODY_CALLS[0] = 0
    ON_SECOND_RUN[0] = "nothing"
    ROLLS.clear()
    BODIES_RUNNING[0] = 0
    BODIES_AT_ONCE[0] = 0
    STEPS_RUNNING[0] = 0
    STEPS_AT_ONCE[0] = 0


async def gate_open() -> None:
    if GATE is not None:
        await GATE.wait()


async def second_gate_open() -> None:
    if SECOND_GATE is not None:
        await SECOND_GATE.wait()


# ---- helpers ----


async def run_job(
    client: LocalProjectClient,
    job: JobID,
    *functions: Any,
    verify_determinism: bool = True,
    limits: RunLimits = RunLimits(),
    settings: RunnerSettings = FAST,
    owner: str = "runner",
    overwrite_cache_conflicts: bool = False,
) -> JobReport:
    """Take the job here and run it to settlement, then report its status. Creates nothing: the
    job and its root invocation are already facts, and taking the job is what creates the job
    attempt (a first one, or the next one, which is what resuming is)."""
    runner = await a_runner(
        client.database,
        job,
        *functions,
        owner=owner,
        limits=limits,
        settings=settings,
        verify_determinism=verify_determinism,
        overwrite_cache_conflicts=overwrite_cache_conflicts,
    )
    await runner.run()
    return await client.jobs.status(job)


async def run_jobs(
    client: LocalProjectClient,
    jobs: Sequence[JobID],
    *functions: Any,
    verify_determinism: bool = True,
    settings: RunnerSettings = FAST,
    alongside: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> list[JobReport]:
    """Run several jobs at once, each in its own runner, and report their statuses in order.
    ``alongside`` runs as another task beside them, for a test that must act while they run."""
    statuses: dict[int, JobReport] = {}

    async def run(index: int, job: JobID) -> None:
        statuses[index] = await run_job(
            client,
            job,
            *functions,
            verify_determinism=verify_determinism,
            settings=settings,
            owner=f"runner-{index}",
        )

    async with anyio.create_task_group() as tg:
        if alongside is not None:
            tg.start_soon(alongside)
        for index, job in enumerate(jobs):
            tg.start_soon(run, index, job)
    return [statuses[index] for index in range(len(jobs))]


async def invocation_is(client: LocalProjectClient, job: JobID, pathname: str, state: str) -> bool:
    """Whether an invocation's derived state is ``state`` (a condition for :func:`wait_until`)."""
    return (await client.jobs.status(job)).invocations[pathname].state == state


async def job_attempt_is(client: LocalProjectClient, job: JobID, state: str) -> bool:
    """Whether the job's current job attempt is in ``state`` (a condition for
    :func:`wait_until`)."""
    return (await client.jobs.status(job)).job_attempt.state == state


async def wait_until_running(
    client: LocalProjectClient, job: JobID, pathname: str = "root"
) -> None:
    """Wait until the invocation has a running attempt (its body is running)."""

    async def running() -> bool:
        status = await client.jobs.status(job)
        return status.invocations[pathname].state == "running"

    await wait_until(running)


async def wait_until_recorded(client: LocalProjectClient, job: JobID, kind: type[Any]) -> None:
    """Wait until the root invocation has a node of ``kind`` on record (the body got that far and
    the recorder committed it). Tests that stop a runner mid-body wait for this, since a running
    attempt alone says nothing about what its body has done."""

    async def recorded() -> bool:
        root = (await client.jobs.describe(job)).invocations["root"]
        return any(isinstance(n.node, kind) for n in root.nodes)

    await wait_until(recorded)


async def wait_until_verifying() -> None:
    """Wait until a counting body has been entered a second time: the verification run
    is in the body."""

    async def verifying() -> bool:
        return BODY_CALLS[0] >= 2

    await wait_until(verifying)


async def step_attempts(
    client: LocalProjectClient, cache_key: str, *jobs: JobID
) -> list[AttemptRecord]:
    """Every step attempt the given jobs made at a cache key, oldest first."""
    found: list[AttemptRecord] = []
    for job in jobs:
        job_attempts = (await client.jobs.status(job)).job_attempts
        async with client.database.transaction() as tx:
            for job_attempt in job_attempts:
                found += [
                    a
                    for a in await execution.list_owned_attempts(tx, job_attempt.id)
                    if a.cache_key == cache_key
                ]
    return sorted(found, key=lambda a: a.started_at)


async def wait_until_step_runs(client: LocalProjectClient, job: JobID, cache_key: str) -> None:
    """Wait until an attempt at the cache key is running."""

    async def running() -> bool:
        described = await client.jobs.describe(job)
        described_step = described.steps.get(cache_key)
        return (
            described_step is not None
            and described_step.attempt is not None
            and described_step.attempt.state == "running"
        )

    await wait_until(running)


async def assert_status_steps_match_describe(client: LocalProjectClient, job: JobID) -> None:
    """Check that the status report's step entries are the displays :meth:`Jobs.describe` builds
    step by step: the report reads them from the step relation in one query, and the two must
    agree on every step's state, answering attempt, and error."""
    status = await client.jobs.status(job)
    described = await client.jobs.describe(job)
    assert status.steps == {key: step.display for key, step in described.steps.items()}


async def current_step_attempt(client: LocalProjectClient, cache_key: str) -> AttemptRecord | None:
    """The attempt behind the cache entry at a key, if there is an entry."""
    async with client.database.transaction() as tx:
        entry = await execution.get_cache_entry(tx, cache_key)
        return None if entry is None else await execution.get_attempt(tx, behind(entry))


# ---- workflows ----


@workflow(inputs={"rows": ROWS_TYPE}, output=("[copy: str, k: int]", int))
async def reshape(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[int]:
    x = rows.field("x").collect()
    row = await rows.sel({"id": 2}).collect().observe()
    assert row.item() == {"x": 20, "y": "q"}
    extra = context.add_source_array("extra", Array.scalar(1, int))
    assert (await extra.observe()).item() == 1
    return context.stack({"one": x, "two": x}, dim="copy").rename({"id": "k"}).collect()


@workflow(inputs={"x": int}, output=int)
async def raising(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    if RAISE[0]:
        raise RuntimeError("boom")
    return x


@workflow(inputs={"x": int}, output=str)
async def wrong_type(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[Any]:
    return x


@workflow(inputs={"a": ("[k: int]", int), "b": ("[k: int]", int)}, output=("[k: int]", int))
async def bad_join_observed(
    context: WorkflowContext, a: ArrayHandle[int], b: ArrayHandle[int]
) -> ArrayHandle[Any]:
    both = context.combine({"a": a, "b": b})  # exact join, mismatched keys: fails when computed
    await both.observe()
    return a


@workflow(inputs={"a": ("[k: int]", int), "b": ("[k: int]", int)}, output=("[k: int]", int))
async def bad_join_unobserved(
    context: WorkflowContext, a: ArrayHandle[int], b: ArrayHandle[int]
) -> ArrayHandle[Any]:
    context.combine({"a": a, "b": b})  # a dead branch, but every node must complete
    return a


@workflow(inputs={"rows": ROWS_TYPE}, output=("[k: int]", int))
async def two_phase(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[int]:
    x = rows.field("x").collect()
    await x.observe()
    await gate_open()
    return x.rename({"id": "k"}).collect()


@workflow(inputs={"rows": ROWS_TYPE}, output=("[k: int]", int))
async def counted(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[int]:
    """``two_phase`` that counts its runs: on its second (its verification run with verification
    mode) it raises, or blocks on the gate, or does neither, per ``ON_SECOND_RUN``."""
    BODY_CALLS[0] += 1
    x = rows.field("x").collect()
    await x.observe()
    if BODY_CALLS[0] == 2:
        match ON_SECOND_RUN[0]:
            case "raise":
                raise RuntimeError("flaky")
            case "block":
                await gate_open()
            case _:
                pass
    return x.rename({"id": "k"}).collect()


@workflow(inputs={}, output=int)
async def changing_source(context: WorkflowContext) -> ArrayHandle[int]:
    a = context.add_source_array("a", Array.scalar(SOURCE_VALUE[0], int))
    await a.observe()
    await gate_open()
    return a


@workflow(inputs={"rows": ROWS_TYPE}, output=int)
async def shrinking(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[int]:
    a = context.add_source_array("a", Array.scalar(1, int))
    if EMIT_EXTRA[0]:
        rows.field("x").collect()
    await a.observe()
    await gate_open()
    return a


@workflow(inputs={}, output=int)
async def nondeterministic(context: WorkflowContext) -> ArrayHandle[int]:
    SOURCE_VALUE[0] += 1  # a different source array on every run of the body
    return context.add_source_array("a", Array.scalar(SOURCE_VALUE[0], int))


# ---- the happy path ----


async def test_root_workflow_runs_end_to_end(client: LocalProjectClient) -> None:
    job = await client.jobs.create(reshape, {"rows": ROWS})
    status = await run_job(client, job, reshape)
    assert status.state == "succeeded"
    assert status.invocations["root"].state == "completed"
    assert await client.jobs.result(job) == Array.from_items(
        [(("one", 1), 10), (("one", 2), 20), (("two", 1), 10), (("two", 2), 20)],
        ("[copy: str, k: int]", int),
    )
    described = await client.jobs.describe(job)
    root = described.invocations["root"]
    kinds = sorted(nodes.kind_of(n.node) for n in root.nodes)
    assert kinds == ["input", "query", "query", "query", "source_array", "stack"]
    # Leaves have values, derived nodes do not.
    assert {nodes.kind_of(n.node) for n in root.nodes if n.value is not None} == {
        "input",
        "source_array",
    }
    (attempt,) = root.attempts
    assert attempt.attempt.state == "completed"
    assert attempt.attempt.owner_job_attempt_id == status.job_attempt.id
    assert [e.kind for e in attempt.events] == [
        "emit",  # the field query
        "emit",  # the sel query
        "observe",
        "emit",  # source array
        "observe",
        "emit",  # stack
        "emit",  # the rename query
        "return",
    ]
    observed = [e for e in attempt.events if e.kind == "observe"]
    sel = one_step_over_rows(root, lambda rows: rows.sel({"id": 2}))
    assert observed[0].identity == sel  # the request names the node; values are derived


async def test_a_job_runs_when_postgres_plans_its_statements_generically(
    database: Database,
) -> None:
    # Postgres plans a prepared statement's first few executions for their values and may then
    # switch to one generic plan, which a pooled connection reaches after a few jobs. Forcing the
    # generic plan from the first execution makes every statement meet it at once.
    def generic_plans(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET plan_cache_mode = force_generic_plan")
        cursor.close()

    event.listen(database.engine.sync_engine, "connect", generic_plans)
    await database.engine.dispose()
    client = a_client(database)

    job = await client.jobs.create(reshape, {"rows": ROWS})
    status = await run_job(client, job, reshape)

    assert status.state == "succeeded"


async def test_status_reports_stopped_then_queued_then_running_then_completed(
    client: LocalProjectClient,
) -> None:
    # A job nobody has taken has no job attempt and nothing is due under one: stopped.
    untaken = await client.jobs.create(reshape, {"rows": ROWS})
    status = await client.jobs.status(untaken)
    assert status.state == "stopped" and status.invocations["root"].state == "stopped"
    assert not status.job_attempts
    global GATE
    GATE = anyio.Event()
    job = await client.jobs.create(two_phase, {"rows": ROWS})
    # Taking the job creates the job attempt: its root is now queued, and the job is running.
    runner = await a_runner(client.database, job, two_phase, verify_determinism=True)
    status = await client.jobs.status(job)
    assert status.state == "running" and status.invocations["root"].state == "queued"
    async with runner_task(runner) as outcome:
        await wait_until_running(client, job)
        status = await client.jobs.status(job)
        assert status.invocations["root"].state == "running"
        assert status.invocations["root"].attempt_id is not None
        GATE.set()
    assert outcome.settled is not None and outcome.settled.state == "succeeded"
    assert (await client.jobs.status(job)).invocations["root"].state == "completed"


async def test_inputs_must_fit_the_signature(client: LocalProjectClient) -> None:
    from fxtr.experiment.signatures import SignatureError

    with pytest.raises(SignatureError):
        await client.jobs.create(reshape, {"rows": Array.scalar(1, int)})


async def test_a_project_array_snapshot_is_a_root_input(client: LocalProjectClient) -> None:
    await client.create_array("rows", ROWS_TYPE)
    await client.upsert_rows("rows", ROWS)
    snapshot = await client.snapshot_array("rows")
    job = await client.jobs.create(reshape, {"rows": snapshot.array})
    status = await run_job(client, job, reshape)
    assert status.state == "succeeded"
    assert await client.jobs.result(job) == Array.from_items(
        [(("one", 1), 10), (("one", 2), 20), (("two", 1), 10), (("two", 2), 20)],
        ("[copy: str, k: int]", int),
    )
    root = (await client.jobs.describe(job)).invocations["root"]
    (given,) = [n.node for n in root.nodes if isinstance(n.node, nodes.Input)]
    assert isinstance(given, nodes.Input)
    assert given.array.entity_id.cid == snapshot.array.entity_id.cid


# ---- failures ----


async def test_body_raising_fails_the_attempt_and_the_job_stops(client: LocalProjectClient) -> None:
    RAISE[0] = True
    job = await client.jobs.create(raising, {"x": Array.scalar(1, int)})
    status = await run_job(client, job, raising)
    assert status.state == "stopped" and status.failed
    assert status.invocations["root"].state == "failed"
    assert status.errors["root"].type_name == "RuntimeError"
    assert status.errors["root"].message == "boom"
    typed = await client.jobs.create(wrong_type, {"x": Array.scalar(1, int)})
    status = await run_job(client, typed, wrong_type)
    assert status.errors["root"].type_name == "SignatureError"
    described = await client.jobs.describe(job)
    (attempt,) = described.invocations["root"].attempts
    assert attempt.attempt.state == "failed" and attempt.attempt.error is not None


async def test_a_failed_derived_node_fails_the_attempt_when_observed_or_at_return(
    client: LocalProjectClient,
) -> None:
    a = Array.from_items([((1,), 1)], ("[k: int]", int))
    b = Array.from_items([((2,), 2)], ("[k: int]", int))
    observed = await client.jobs.create(bad_join_observed, {"a": a, "b": b})
    status = await run_job(client, observed, bad_join_observed)
    assert status.failed and status.errors["root"].type_name == "ArrayError"
    unobserved = await client.jobs.create(bad_join_unobserved, {"a": a, "b": b})
    status = await run_job(client, unobserved, bad_join_unobserved)
    assert status.failed and status.errors["root"].type_name == "ArrayError"
    for job in (observed, unobserved):
        root = (await client.jobs.describe(job)).invocations["root"]
        combine = next(n for n in root.nodes if isinstance(n.node, nodes.Combine))
        assert root.invocation.result is None
        # The failed node is named on the attempt; its error is not stored on the node.
        (attempt,) = root.attempts
        assert attempt.attempt.stopped_on == (combine.identity,)
    # The observe of the failed node is on record as a request, after the emission that made it.
    (attempt,) = (await client.jobs.describe(observed)).invocations["root"].attempts
    assert [(e.kind, e.identity) for e in attempt.events] == [
        ("emit", combine.identity),
        ("observe", combine.identity),
    ]


# ---- determinism verification ----


async def test_verification_fails_a_nondeterministic_body_within_its_attempt(
    client: LocalProjectClient,
) -> None:
    job = await client.jobs.create(nondeterministic, {})
    status = await run_job(client, job, nondeterministic)
    assert status.failed and status.errors["root"].type_name == "DeterminismError"
    root = (await client.jobs.describe(job)).invocations["root"]
    (attempt,) = root.attempts  # the attempt that produced the result is the one that failed
    assert attempt.attempt.state == "failed"
    # Only the first pass reached the ledger: one source array, holding the first value.
    sources = [n for n in root.nodes if isinstance(n.node, nodes.SourceArray)]
    assert len(sources) == 1 and sources[0].value is not None
    assert (await client.entities.load(sources[0].value)).item() == 2
    assert [e.kind for e in attempt.events] == ["emit"]


async def test_verification_verifies_with_a_second_run_and_records_once(
    client: LocalProjectClient,
) -> None:
    job = await client.jobs.create(counted, {"rows": ROWS})
    assert (await run_job(client, job, counted)).state == "succeeded"
    assert BODY_CALLS[0] == 2
    root = (await client.jobs.describe(job)).invocations["root"]
    (attempt,) = root.attempts  # the verification run is not an attempt of the ledger
    assert attempt.attempt.state == "completed"
    # The verification run recorded nothing: the attempt's events are its body's, plus the one
    # return recorded once the run agreed.
    assert [e.kind for e in attempt.events] == ["emit", "observe", "emit", "return"]


async def test_a_body_raising_only_on_the_verification_run_is_a_violation(
    client: LocalProjectClient,
) -> None:
    ON_SECOND_RUN[0] = "raise"
    job = await client.jobs.create(counted, {"rows": ROWS})
    status = await run_job(client, job, counted)
    assert status.failed and status.errors["root"].type_name == "DeterminismError"
    assert "raised on the verification run" in status.errors["root"].message
    assert "RuntimeError: flaky" in status.errors["root"].message
    (attempt,) = (await client.jobs.describe(job)).invocations["root"].attempts
    assert attempt.attempt.state == "failed"
    assert [e.kind for e in attempt.events] == ["emit", "observe", "emit"]  # no return recorded


async def test_a_runner_stopped_during_a_verification_run_abandons_the_attempt(
    client: LocalProjectClient,
) -> None:
    global GATE
    GATE = anyio.Event()  # never opened: the verification run blocks
    ON_SECOND_RUN[0] = "block"
    job = await client.jobs.create(counted, {"rows": ROWS})
    runner = await a_runner(client.database, job, counted, verify_determinism=True)
    async with runner_task(runner) as outcome:
        await wait_until_verifying()
        outcome.kill()
    # The runner stopped while the verification run was blocked: the attempt, whose return was
    # held, is abandoned like any the runner stops running, and its job attempt is settled, so a
    # resume needs no takeover.
    assert outcome.settled is None  # cancelled from outside, so it returned nothing
    status = await client.jobs.status(job)
    assert status.job_attempt.state == "cancelled" and not status.job_attempt.evicted
    # Idle under a cancelled job attempt, with no attempt of that episode left to explain it.
    assert status.invocations["root"].state == "stopped"
    GATE = None
    ON_SECOND_RUN[0] = "nothing"
    BODY_CALLS[0] = 0
    assert (await run_job(client, job, counted)).state == "succeeded"
    first, second = (await client.jobs.describe(job)).invocations["root"].attempts
    assert first.attempt.state == "abandoned" and first.attempt.reason == "the runner stopped"
    assert [e.kind for e in first.events] == ["emit", "observe", "emit"]  # its return was held
    assert second.attempt.state == "completed"
    assert [e.kind for e in second.events] == ["emit", "observe", "emit", "return"]


# ---- stopping abruptly, resuming, determinism ----


async def test_work_abandoned_by_a_stopped_runner_is_resumed_from_the_ledger(
    client: LocalProjectClient,
) -> None:
    global GATE
    GATE = anyio.Event()  # never opened: the first attempt blocks after observing
    job = await client.jobs.create(two_phase, {"rows": ROWS})
    runner = await a_runner(client.database, job, two_phase, verify_determinism=True)
    async with runner_task(runner) as outcome:
        await wait_until_recorded(client, job, nodes.Query)
        outcome.kill()
    GATE = None
    assert (await run_job(client, job, two_phase)).state == "succeeded"
    described = await client.jobs.describe(job)
    root = described.invocations["root"]
    first, second = root.attempts
    assert first.attempt.state == "abandoned" and first.attempt.reason == "the runner stopped"
    assert second.attempt.state == "completed"
    assert first.attempt.owner_job_attempt_id != second.attempt.owner_job_attempt_id
    # Attempt one recorded the field node; attempt two emitted it again (a match) and went on.
    field_id = one_step_over_rows(root, lambda rows: rows.field("x"))
    field = next(n for n in root.nodes if n.identity == field_id)
    assert ("emit", field.identity) in [(e.kind, e.identity) for e in first.events]
    assert [e.kind for e in second.events] == ["emit", "observe", "emit", "return"]
    assert await client.jobs.result(job) == Array.from_items(
        [((1,), 10), ((2,), 20)], ("[k: int]", int)
    )


async def test_a_runner_cancelled_from_outside_abandons_its_live_attempts(
    client: LocalProjectClient,
) -> None:
    """The abrupt stop (the process killed, the runner's task cancelled) leaves nothing running:
    the item task abandons its attempt and the runner settles its job attempt on the way out."""
    global GATE
    GATE = anyio.Event()  # never opened: the body blocks after observing
    job = await client.jobs.create(two_phase, {"rows": ROWS})
    runner = await a_runner(client.database, job, two_phase)
    async with runner_task(runner) as outcome:
        await wait_until_running(client, job)
        outcome.kill()
    described = await client.jobs.describe(job)
    (abandoned,) = described.invocations["root"].attempts
    assert abandoned.attempt.state == "abandoned"
    assert abandoned.attempt.reason == "the runner stopped"
    (job_attempt,) = described.job_attempts
    assert job_attempt.state == "cancelled" and not job_attempt.evicted


async def test_a_changed_body_is_a_determinism_violation(client: LocalProjectClient) -> None:
    global GATE
    GATE = anyio.Event()
    renamed = await client.jobs.create(changing_source, {})
    shrunk = await client.jobs.create(shrinking, {"rows": ROWS})
    for job, seen in ((renamed, nodes.SourceArray), (shrunk, nodes.Query)):
        runner = await a_runner(client.database, job, changing_source, shrinking)
        async with runner_task(runner) as outcome:
            await wait_until_recorded(client, job, seen)
            outcome.kill()
    GATE = None
    SOURCE_VALUE[0] = 2  # the same name "a" now carries different content
    EMIT_EXTRA[0] = False  # the field node an earlier attempt emitted is never emitted
    status = await run_job(client, renamed, changing_source, shrinking)
    assert status.failed and status.errors["root"].type_name == "DeterminismError"
    assert "Name 'a'" in status.errors["root"].message
    status = await run_job(client, shrunk, changing_source, shrinking)
    assert status.failed and status.errors["root"].type_name == "DeterminismError"
    assert "without emitting" in status.errors["root"].message


async def test_resume_makes_a_failed_invocation_eligible_again(client: LocalProjectClient) -> None:
    RAISE[0] = True
    job = await client.jobs.create(raising, {"x": Array.scalar(7, int)})
    assert (await run_job(client, job, raising)).failed
    with pytest.raises(KeyError):
        await client.jobs.resume(
            JobID(uuid.uuid4()),
            owner="somebody",
            code_version=await a_code_version(client.database),
        )
    RAISE[0] = False
    # Taking the job again is the resume: the next job attempt may try the failed invocation once.
    assert (await run_job(client, job, raising)).state == "succeeded"
    with pytest.raises(NotResumableError):
        await client.jobs.resume(  # finished
            job, owner="somebody", code_version=await a_code_version(client.database)
        )
    attempts = (await client.jobs.describe(job)).invocations["root"].attempts
    assert [a.attempt.state for a in attempts] == ["failed", "completed"]


# ---- child workflows ----

IDS_TYPE = ArrayType.of(("[id: int]", int))
IDS = Array.from_items([((1,), 10), ((2,), 20)], IDS_TYPE)
FOUR_IDS = Array.from_items([((i,), 10 * i) for i in (1, 2, 3, 4)], IDS_TYPE)
TAGGED_TYPE = ArrayType.of(("[id: int, copy: str]", int))


@workflow(inputs={"x": int}, output=("[copy: str]", int))
async def tag(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    """A child: two copies of its scalar input along a new dim."""
    if RAISE[0]:
        raise RuntimeError("child boom")
    return context.stack({"a": x, "b": x}, dim="copy")


@workflow(inputs={"ids": IDS_TYPE}, output=TAGGED_TYPE)
async def tag_each(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    """A parent: the child mapped over ``id``, returned without looking at it."""
    return context.run_child_workflow("tag", tag, {"x": ids}, map_over=["id"])


@workflow(inputs={"ids": IDS_TYPE}, output=int)
async def tag_and_observe(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    """A parent that waits for its children's results mid-body, then goes on."""
    tagged = context.run_child_workflow("tag", tag, {"x": ids}, map_over=["id"])
    seen = await tagged.observe()
    assert (await seen.sel({"id": 2, "copy": "b"}).collect()).item() == 20
    await gate_open()
    return tagged.sel({"id": 1, "copy": "a"}).collect()


@workflow(inputs={"ids": IDS_TYPE}, output=("[id: int, rep: int, copy: str]", int))
async def tag_with_replicas(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_child_workflow(
        "tag", tag, {"x": ids}, map_over=["id"], replicas={"rep": [0, 1]}
    )


async def test_child_workflows_run_and_nest_pathnames(client: LocalProjectClient) -> None:
    job = await client.jobs.create(tag_each, {"ids": IDS})
    status = await run_job(client, job, tag_each, tag)
    assert status.state == "succeeded", status
    assert await client.jobs.result(job) == Array.from_items(
        [((1, "a"), 10), ((1, "b"), 10), ((2, "a"), 20), ((2, "b"), 20)], TAGGED_TYPE
    )
    assert {path: s.state for path, s in status.invocations.items()} == {
        "root": "completed",
        "root/tag[id:1]": "completed",
        "root/tag[id:2]": "completed",
    }
    described = await client.jobs.describe(job)
    root = described.invocations["root"]
    run = next(n for n in root.nodes if isinstance(n.node, nodes.RunChildWorkflow))
    assert [(s.node_identity, s.key) for s in root.spawns] == [
        (run.identity, (1,)),
        (run.identity, (2,)),
    ]
    child = described.invocations["root/tag[id:1]"]
    assert child.invocation.workflow_name == tag.name
    assert child.invocation.cache_key_prefix == "root/tag[id:1]"  # the child's default prefix
    (x,) = [n for n in child.nodes if isinstance(n.node, nodes.Input)]
    assert x.value is not None and (await client.entities.load(x.value)) == Array.scalar(10, int)
    assert not child.spawns


async def test_a_parent_can_observe_its_children_and_replicas_multiply_them(
    client: LocalProjectClient,
) -> None:
    observed = await client.jobs.create(tag_and_observe, {"ids": IDS})
    assert (await run_job(client, observed, tag_and_observe, tag)).state == "succeeded"
    replicated = await client.jobs.create(tag_with_replicas, {"ids": IDS})
    assert (await run_job(client, replicated, tag_with_replicas, tag)).state == "succeeded"
    # The parent's observe was served once the children finished.
    assert (await client.jobs.result(observed)).item() == 10
    # Replicas run the same child once per replica value under distinct pathnames.
    assert sorted((await client.jobs.describe(replicated)).invocations) == [
        "root",
        "root/tag[id:1,rep:0]",
        "root/tag[id:1,rep:1]",
        "root/tag[id:2,rep:0]",
        "root/tag[id:2,rep:1]",
    ]
    assert len(await client.jobs.result(replicated)) == 8


async def test_an_empty_mapped_key_set_spawns_nothing(client: LocalProjectClient) -> None:
    job = await client.jobs.create(tag_each, {"ids": Array.from_items([], IDS_TYPE)})
    assert (await run_job(client, job, tag_each, tag)).state == "succeeded"
    assert await client.jobs.result(job) == Array.from_items([], TAGGED_TYPE)
    assert list((await client.jobs.describe(job)).invocations) == ["root"]


async def test_a_missing_function_fails_the_requesting_attempt(client: LocalProjectClient) -> None:
    """A runner whose registry lacks the child's workflow fails the child's attempt, which
    interrupts the parent: there is no other process to hand the work to."""
    job = await client.jobs.create(tag_each, {"ids": IDS})
    status = await run_job(client, job, tag_each)  # no `tag`
    assert status.state == "stopped"
    assert status.invocations["root"].state == "interrupted"
    for pathname in ("root/tag[id:1]", "root/tag[id:2]"):
        assert status.invocations[pathname].state == "failed"
        assert status.errors[pathname].type_name == "RegistryError"


async def test_a_failed_child_interrupts_the_parent_and_resume_cascades(
    client: LocalProjectClient,
) -> None:
    RAISE[0] = True
    job = await client.jobs.create(tag_and_observe, {"ids": IDS})
    status = await run_job(client, job, tag_and_observe, tag)
    assert status.state == "stopped" and status.failed
    root = status.invocations["root"]
    assert root.state == "interrupted"
    # Both children fail; the parent is interrupted as soon as one of them has, and names it.
    assert root.reason is not None and "failed" in root.reason
    assert any(f"child invocation 'root/tag[id:{i}]'" in root.reason for i in (1, 2))
    assert status.invocations["root/tag[id:1]"].state == "failed"
    assert status.errors["root/tag[id:1]"].message == "child boom"
    described = await client.jobs.describe(job)
    run = next(
        n for n in described.invocations["root"].nodes if isinstance(n.node, nodes.RunChildWorkflow)
    )
    (attempt,) = described.invocations["root"].attempts
    assert attempt.attempt.state == "interrupted"
    assert attempt.attempt.error is None and attempt.attempt.stopped_on == (run.identity,)
    # Fix the child and take the job again: the failed children are resumed with the parent,
    # nothing is spawned again, and the job completes.
    RAISE[0] = False
    assert (await run_job(client, job, tag_and_observe, tag)).state == "succeeded"
    described = await client.jobs.describe(job)
    assert sorted(described.invocations) == ["root", "root/tag[id:1]", "root/tag[id:2]"]
    assert len(described.invocations["root"].attempts) == 2
    assert [a.attempt.state for a in described.invocations["root/tag[id:1]"].attempts] == [
        "failed",
        "completed",
    ]


async def test_a_parent_stopped_after_expanding_does_not_spawn_again(
    client: LocalProjectClient,
) -> None:
    global GATE
    GATE = anyio.Event()  # the parent blocks after observing its children
    job = await client.jobs.create(tag_and_observe, {"ids": IDS})
    runner = await a_runner(client.database, job, tag_and_observe, tag)
    async with runner_task(runner) as outcome:

        async def children_done() -> bool:
            status = await client.jobs.status(job)
            children = [s for p, s in status.invocations.items() if p != "root"]
            return len(children) == 2 and all(s.state == "completed" for s in children)

        await wait_until(children_done)
        await wait_until_recorded(client, job, nodes.RunChildWorkflow)
        outcome.kill()
    GATE = None
    assert (await run_job(client, job, tag_and_observe, tag)).state == "succeeded"
    described = await client.jobs.describe(job)
    assert sorted(described.invocations) == ["root", "root/tag[id:1]", "root/tag[id:2]"]
    first, second = described.invocations["root"].attempts
    assert first.attempt.state == "abandoned" and second.attempt.state == "completed"
    assert len(described.invocations["root"].spawns) == 2


# ---- steps and the cache ----


@step(inputs={"seed": str}, output=int)
async def roll(context: StepContext, seed: str) -> int:
    """A nondeterministic step: a fresh value every time it runs, whatever its input."""
    await gate_open()
    if RAISE[0]:
        raise RuntimeError("step boom")
    ROLLS.append(seed)
    return 1000 + len(ROLLS)


@step(inputs={"seed": str}, output=int)
async def second_roll(context: StepContext, seed: str) -> int:
    """A second nondeterministic step, on its own gate, so a test can hold one of two steps open
    while the other finishes."""
    await second_gate_open()
    ROLLS.append(seed)
    return 2000 + len(ROLLS)


@workflow(inputs={"seed": str}, output=int)
async def roll_once(context: WorkflowContext, seed: ArrayHandle[str]) -> ArrayHandle[int]:
    return context.run_step("roll", roll, {"seed": seed})


@workflow(inputs={"seed": str}, output=struct_of(a=int, b=int, c=int))
async def roll_shared(context: WorkflowContext, seed: ArrayHandle[str]) -> ArrayHandle[Any]:
    """Two requests at one key are one call; a third at another key is another call."""
    a = context.run_step("a", roll, {"seed": seed}, override_cache_address_prefix="shared/roll")
    b = context.run_step("b", roll, {"seed": seed}, override_cache_address_prefix="shared/roll")
    c = context.run_step("c", roll, {"seed": seed})
    return context.combine({"a": a, "b": b, "c": c})


@workflow(inputs={"seeds": ("[seed: str]", str)}, output=("[seed: str]", int))
async def roll_each(context: WorkflowContext, seeds: ArrayHandle[str]) -> ArrayHandle[int]:
    return context.run_step("roll", roll, {"seed": seeds}, map_over=["seed"])


@workflow(inputs={"seed": str}, output=struct_of(one=int, two=int))
async def two_rolls(context: WorkflowContext, seed: ArrayHandle[str]) -> ArrayHandle[Any]:
    """Two steps at once, each on its own gate."""
    one = context.run_step("one", roll, {"seed": seed})
    two = context.run_step("two", second_roll, {"seed": seed})
    return context.combine({"one": one, "two": two})


@workflow(inputs={}, output=struct_of(a=int, b=int))
async def conflicting_rolls(context: WorkflowContext) -> ArrayHandle[Any]:
    """One key for two different requests: the body's own fault."""
    a = context.run_step(
        "a",
        roll,
        {"seed": context.add_source_array("s1", Array.scalar("s1", str))},
        override_cache_address_prefix="shared/roll",
    )
    b = context.run_step(
        "b",
        roll,
        {"seed": context.add_source_array("s2", Array.scalar("s2", str))},
        override_cache_address_prefix="shared/roll",
    )
    return context.combine({"a": a, "b": b})


@workflow(inputs={"seeds": ("[id: int]", str)}, output=("[id: int]", int))
async def roll_and_again(context: WorkflowContext, seeds: ArrayHandle[str]) -> ArrayHandle[int]:
    """Two mapped steps over one input, so a changed input conflicts at keys of two shapes."""
    context.run_step("again", second_roll, {"seed": seeds}, map_over=["id"])
    return context.run_step("roll", roll, {"seed": seeds}, map_over=["id"])


def seeds_by_id(*seeds: str) -> Array[str]:
    return Array.from_items([((i,), seed) for i, seed in enumerate(seeds)], ("[id: int]", str))


SEED = Array.scalar("x", str)
ROLL_KEY = "root/roll"


async def test_steps_run_and_a_key_names_one_call(client: LocalProjectClient) -> None:
    shared = await client.jobs.create(roll_shared, {"seed": SEED})
    assert (await run_job(client, shared, roll_shared, roll)).state == "succeeded"
    seeds = Array.from_items([(("p",), "p"), (("q",), "q")], ("[seed: str]", str))
    each = await client.jobs.create(roll_each, {"seeds": seeds})
    assert (await run_job(client, each, roll_each, roll)).state == "succeeded"
    r = (await client.jobs.result(shared)).item()
    assert r["a"] == r["b"] and r["a"] != r["c"]  # the shared key ran once
    described = await client.jobs.describe(shared)
    assert sorted(described.steps) == ["root/c", "shared/roll"]
    assert all(
        d.attempt is not None and d.attempt.state == "completed" for d in described.steps.values()
    )
    mapped = await client.jobs.result(each)
    assert sorted(mapped.dim_names) == ["seed"] and len(mapped) == 2
    assert sorted((await client.jobs.describe(each)).steps) == [
        'root/roll[seed:"p"]',
        'root/roll[seed:"q"]',
    ]
    assert len(ROLLS) == 4


async def test_two_jobs_share_one_step_attempt_and_a_clear_leaves_bindings_intact(
    client: LocalProjectClient,
) -> None:
    first = await client.jobs.create(roll_once, {"seed": SEED})
    assert (await run_job(client, first, roll_once, roll)).state == "succeeded"
    second = await client.jobs.create(roll_once, {"seed": SEED})
    assert (await run_job(client, second, roll_once, roll)).state == "succeeded"
    # The second job hit the cache: the same attempt, the same value, the step ran once.
    assert await client.jobs.result(first) == await client.jobs.result(second)
    assert len(ROLLS) == 1
    bound_a = (await client.jobs.describe(first)).steps[ROLL_KEY].binding
    bound_b = (await client.jobs.describe(second)).steps[ROLL_KEY].binding
    assert bound_a is not None and bound_b is not None
    assert bound_a.step_result_id == bound_b.step_result_id
    # Clearing the key sends the next job to a fresh attempt; the earlier bindings stay put.
    assert await client.clear_cache_entries(addresses=[ROLL_KEY]) == 1
    third = await client.jobs.create(roll_once, {"seed": SEED})
    assert (await run_job(client, third, roll_once, roll)).state == "succeeded"
    assert len(ROLLS) == 2
    bound_c = (await client.jobs.describe(third)).steps[ROLL_KEY].binding
    assert bound_c is not None and bound_c.step_result_id != bound_a.step_result_id
    assert (await client.jobs.describe(first)).steps[ROLL_KEY].binding == bound_a
    assert await client.jobs.result(first) != await client.jobs.result(third)


async def test_results_carry_their_provenance_in_the_ledger(client: LocalProjectClient) -> None:
    """A completed step attempt leaves a step result naming the job attempt that ran it, and the
    cache entry offers that step result; an invocation's result names its producer too. Both
    survive the loss of the producing job's execution rows, as for a result from elsewhere."""
    first = await client.jobs.create(roll_once, {"seed": SEED})
    assert (await run_job(client, first, roll_once, roll)).state == "succeeded"
    second = await client.jobs.create(roll_once, {"seed": SEED})
    assert (await run_job(client, second, roll_once, roll)).state == "succeeded"  # a cache hit
    async with client.database.transaction() as tx:
        (producer,) = await execution.list_job_attempts(tx, first)
        entry = await execution.get_cache_entry(tx, ROLL_KEY)
        assert entry is not None and entry.attempt_id is None and entry.step_result_id is not None
        step_result = await ledger.get_step_result(tx, entry.step_result_id)
        assert step_result.job_attempt_id == producer.id
        assert (await ledger.find_invocation(tx, first, "root")).job_attempt_id == producer.id
    # Drop the first job's execution state: its attempts and its job attempt's episode row.
    async with client.database.transaction() as tx:
        await tx.execute(attempts.delete().where(attempts.c.owner_job_attempt_id == producer.id))
        await tx.execute(job_attempts.delete().where(job_attempts.c.id == producer.id))
    step = (await client.jobs.describe(second)).steps[ROLL_KEY]
    assert step.attempt is None and step.logs == []
    # The display names no attempt this database cannot load.
    assert step.display.state == "completed" and step.display.attempt_id is None
    assert step.result_provenance is not None
    assert step.result_provenance.job_attempt.id == producer.id
    assert step.result_provenance.job_attempt.job_id == first
    root = (await client.jobs.describe(first)).invocations["root"]
    assert root.attempts == [] and root.result_provenance is not None
    assert root.result_provenance.job_attempt.id == producer.id
    assert root.result_provenance.code_version.commit == TEST_CODE_VERSION.commit


async def test_two_runners_run_two_jobs_concurrently_and_share_a_step(
    client: LocalProjectClient,
) -> None:
    """Two jobs of one workflow request one cache key with equal requests: whichever runner starts
    the attempt runs it, the other waits on it and binds its own request to the result."""
    global GATE
    GATE = anyio.Event()
    first = await client.jobs.create(roll_once, {"seed": SEED})
    second = await client.jobs.create(roll_once, {"seed": SEED})

    async def release() -> None:
        # Let both jobs reach the key before the one attempt completes.
        await wait_until_step_runs(client, first, ROLL_KEY)
        await wait_until_step_runs(client, second, ROLL_KEY)
        assert GATE is not None
        GATE.set()

    statuses = await run_jobs(client, [first, second], roll_once, roll, alongside=release)
    assert [s.state for s in statuses] == ["succeeded", "succeeded"]
    assert len(ROLLS) == 1  # one call, two jobs
    bound_a = (await client.jobs.describe(first)).steps[ROLL_KEY].binding
    bound_b = (await client.jobs.describe(second)).steps[ROLL_KEY].binding
    assert bound_a is not None and bound_b is not None
    assert bound_a.step_result_id == bound_b.step_result_id
    assert await client.jobs.result(first) == await client.jobs.result(second)


async def test_a_failed_step_interrupts_every_job_waiting_on_it_and_each_resumes(
    client: LocalProjectClient,
) -> None:
    global GATE
    RAISE[0] = True
    GATE = anyio.Event()  # the step blocks until both jobs wait on the one attempt
    first = await client.jobs.create(roll_once, {"seed": SEED})
    second = await client.jobs.create(roll_once, {"seed": SEED})

    async def release() -> None:
        await wait_until_step_runs(client, first, ROLL_KEY)
        await wait_until_step_runs(client, second, ROLL_KEY)
        for job in (first, second):
            await assert_status_steps_match_describe(client, job)  # running
        assert GATE is not None
        GATE.set()

    statuses = await run_jobs(client, [first, second], roll_once, roll, alongside=release)
    for job in (first, second):
        await assert_status_steps_match_describe(client, job)  # failed
    for status in statuses:
        assert status.state == "stopped" and status.failed
        root = status.invocations["root"]
        assert root.state == "interrupted"
        assert root.reason is not None and ROLL_KEY in root.reason
        # The job's own failed attempt at the key is what its status shows, stopped or not.
        assert status.steps[ROLL_KEY].state == "failed"
        assert status.step_errors[ROLL_KEY].message == "step boom"
    assert not ROLLS  # nothing was produced
    # A failed attempt holds nothing, so the job that was only waiting took the key over and ran
    # its own attempt, which failed in the same way.
    attempts = await step_attempts(client, ROLL_KEY, first, second)
    assert attempts and all(a.state == "failed" for a in attempts)
    assert all(a.error is not None and a.error.message == "step boom" for a in attempts)
    # Fix the step and take each job again: the first runs the step, the second binds to it.
    RAISE[0] = False
    assert (await run_job(client, first, roll_once, roll)).state == "succeeded"
    assert (await run_job(client, second, roll_once, roll)).state == "succeeded"
    assert len(ROLLS) == 1
    bound_first = (await client.jobs.describe(first)).steps[ROLL_KEY].binding
    bound_second = (await client.jobs.describe(second)).steps[ROLL_KEY].binding
    assert bound_first is not None and bound_second is not None
    assert bound_second.step_result_id == bound_first.step_result_id
    assert await client.jobs.result(first) == await client.jobs.result(second)


async def test_a_cache_conflict_interrupts_until_cleared(
    client: LocalProjectClient,
) -> None:
    first = await client.jobs.create(roll_once, {"seed": SEED})
    assert (await run_job(client, first, roll_once, roll)).state == "succeeded"
    other = await client.jobs.create(roll_once, {"seed": Array.scalar("y", str)})
    status = await run_job(client, other, roll_once, roll)
    assert status.state == "stopped" and status.invocations["root"].state == "interrupted"
    reason = status.invocations["root"].reason
    assert reason is not None and "cache conflict at" in reason
    # The request is a fact; nothing will run it there until the key is cleared.
    conflict = (await client.jobs.describe(other)).steps[ROLL_KEY]
    assert conflict.binding is None
    assert isinstance(conflict.status, execution.Stopped) and conflict.status.cache_conflict
    await assert_status_steps_match_describe(client, other)  # in conflict
    await assert_status_steps_match_describe(client, first)  # completed
    assert await client.clear_cache_entries(addresses=[ROLL_KEY]) == 1
    assert (await run_job(client, other, roll_once, roll)).state == "succeeded"
    assert len(ROLLS) == 2
    first_step = (await client.jobs.describe(first)).steps[ROLL_KEY]
    assert first_step.attempt is not None and first_step.attempt.state == "completed"


async def test_a_jobs_cache_conflicts_are_listed_and_summarized(
    client: LocalProjectClient, capsys: pytest.CaptureFixture[str]
) -> None:
    first = await client.jobs.create(roll_and_again, {"seeds": seeds_by_id("a", "b", "c")})
    assert (await run_job(client, first, roll_and_again, roll, second_roll)).state == "succeeded"
    names = {key: s.step_name for key, s in (await client.jobs.status(first)).steps.items()}
    # Two of the three inputs change: each step conflicts at those two keys, and nowhere else.
    other = await client.jobs.create(roll_and_again, {"seeds": seeds_by_id("a", "x", "y")})
    report = await run_job(client, other, roll_and_again, roll, second_roll)
    assert report.state == "stopped"
    keys = ["root/again[id:1]", "root/again[id:2]", "root/roll[id:1]", "root/roll[id:2]"]
    assert report.cache_conflicts == [CacheConflict(k, names[k], "completed") for k in keys]
    # Each run node's conflicts are one clause of the reason, however many keys it has.
    reason = report.invocations["root"].reason
    assert reason is not None and reason.count("cache conflicts at 2 addresses, such as") == 2
    assert "'root/roll[id:1]'" in reason and "root/roll[id:2]" not in reason
    status = await (await client.job(other)).status()
    assert status.cache_conflicts == report.cache_conflicts and status.reason == reason
    entries = [entry.cache_address for entry in await client.list_cache_entries()]
    assert format_cache_conflicts(status.cache_conflicts, entries) == [
        "cache conflicts at 4 addresses:",
        f"  root/again[id:*]  2 of 3 entries  {names['root/again[id:1]']}",
        f"  root/roll[id:*]   2 of 3 entries  {names['root/roll[id:1]']}",
    ]
    # Each printed pattern selects the entries it counts.
    for pattern in conflict_patterns(status.cache_conflicts):
        assert len(await client.list_cache_entries(str(pattern))) == 3
    # The status report fetches the entries under each group's prefix itself.
    capsys.readouterr()
    await report_job_status(client, status)
    printed = capsys.readouterr().out
    assert "  cache conflicts at 4 addresses:\n" in printed
    assert f"    root/roll[id:*]   2 of 3 entries  {names['root/roll[id:1]']}\n" in printed
    assert f"  to clear them:         fxtr cache clear --conflicts-in {other}" in printed
    # The job's conflicts can be listed, then cleared: exactly them, and not the entries under
    # the same prefix that it does not conflict with.
    listed = await client.list_cache_entries(conflicts_in=other)
    assert sorted(entry.cache_address for entry in listed) == keys
    rolls = await client.list_cache_entries("root/roll", conflicts_in=other)
    assert sorted(e.cache_address for e in rolls) == ["root/roll[id:1]", "root/roll[id:2]"]
    assert await client.clear_cache_entries(conflicts_in=other) == 4
    remaining = sorted(entry.cache_address for entry in await client.list_cache_entries())
    assert remaining == ["root/again[id:0]", "root/roll[id:0]"]
    assert (await run_job(client, other, roll_and_again, roll, second_roll)).state == "succeeded"
    assert await client.list_cache_entries(conflicts_in=other) == []
    with pytest.raises(JobNotFoundError):
        await client.clear_cache_entries(conflicts_in=JobID(uuid.uuid4()))


LENGTHS: list[str] = []
SQUARES: list[int] = []


@step(inputs={"seed": str}, output=int)
async def length(context: StepContext, seed: str) -> int:
    """A deterministic step: the length of the seed."""
    LENGTHS.append(seed)
    return len(seed)


@step(inputs={"n": int}, output=int)
async def square(context: StepContext, n: int) -> int:
    """A deterministic step after ``length``."""
    SQUARES.append(n)
    return n * n


@workflow(inputs={"seeds": ("[id: int]", str)}, output=("[id: int]", int))
async def measure(context: WorkflowContext, seeds: ArrayHandle[str]) -> ArrayHandle[int]:
    lengths = context.run_step("length", length, {"seed": seeds}, map_over=["id"])
    return context.run_step("square", square, {"n": lengths}, map_over=["id"])


async def test_overwriting_cache_conflicts_reruns_what_changed_in_one_resume(
    client: LocalProjectClient,
) -> None:
    LENGTHS.clear()
    SQUARES.clear()
    functions = (measure, length, square)
    first = await client.jobs.create(measure, {"seeds": seeds_by_id("a", "b", "c")})
    assert (await run_job(client, first, *functions)).state == "succeeded"
    before = await client.jobs.result(first)
    # Two seeds change. "x" has the length of "b", so the square after it is the same request as
    # before; "yy" has a new length, so the square after it conflicts once the length reruns.
    other = await client.jobs.create(measure, {"seeds": seeds_by_id("a", "x", "yy")})
    stopped = await run_job(client, other, *functions)
    assert [c.cache_address for c in stopped.cache_conflicts] == [
        "root/length[id:1]",
        "root/length[id:2]",
    ]
    LENGTHS.clear()
    SQUARES.clear()
    resumed = await run_job(client, other, *functions, overwrite_cache_conflicts=True)
    assert resumed.state == "succeeded" and resumed.cache_conflicts == []
    assert sorted(LENGTHS) == ["x", "yy"] and SQUARES == [2]
    assert await client.jobs.result(other) == Array.from_items(
        [((0,), 1), ((1,), 1), ((2,), 4)], ("[id: int]", int)
    )
    # Only the job attempt that asked overwrites.
    assert [ja.overwrite_cache_conflicts for ja in resumed.job_attempts] == [False, True]
    # The first job keeps the results it took, and its provenance.
    assert await client.jobs.result(first) == before
    bound = (await client.jobs.describe(first)).steps["root/length[id:1]"].binding
    assert bound is not None
    async with client.database.transaction() as tx:
        entry = await execution.get_cache_entry(tx, "root/length[id:1]")
    assert entry is not None and entry.step_result_id != bound.step_result_id
    # A job that does not ask finds the conflicts again, now with the second job's results.
    third = await client.jobs.create(measure, {"seeds": seeds_by_id("a", "b", "c")})
    report = await run_job(client, third, *functions)
    assert [c.cache_address for c in report.cache_conflicts] == [
        "root/length[id:1]",
        "root/length[id:2]",
    ]


async def test_overwriting_leaves_a_conflict_with_a_running_attempt(
    client: LocalProjectClient,
) -> None:
    global GATE
    GATE = anyio.Event()
    holder = await client.jobs.create(roll_once, {"seed": SEED})
    holding = await a_runner(client.database, holder, roll_once, roll)
    async with runner_task(holding):
        await wait_until(lambda: _has_running_attempt(client, ROLL_KEY))
        other = await client.jobs.create(roll_once, {"seed": Array.scalar("y", str)})
        report = await run_job(client, other, roll_once, roll, overwrite_cache_conflicts=True)
        assert report.state == "stopped"
        assert [(c.cache_address, c.kind) for c in report.cache_conflicts] == [
            (ROLL_KEY, "running")
        ]
        GATE.set()
    assert (await client.jobs.status(holder)).state == "succeeded"
    # Once the other job's result is completed, a resume that overwrites takes the key.
    resumed = await run_job(client, other, roll_once, roll, overwrite_cache_conflicts=True)
    assert resumed.state == "succeeded"


async def _has_running_attempt(client: LocalProjectClient, cache_key: str) -> bool:
    async with client.database.transaction() as tx:
        entry = await execution.get_cache_entry(tx, cache_key)
        if entry is None or entry.attempt_id is None:
            return False
        return (await execution.get_attempt(tx, entry.attempt_id)).state == "running"


async def test_one_key_for_two_requests_in_one_job_fails_the_attempt(
    client: LocalProjectClient,
) -> None:
    job = await client.jobs.create(conflicting_rolls, {})
    status = await run_job(client, job, conflicting_rolls, roll)
    assert status.failed and status.invocations["root"].state == "failed"
    assert status.errors["root"].type_name == "CacheAddressReuseError"


async def test_a_pending_job_takes_over_an_evicted_step(client: LocalProjectClient) -> None:
    """A job waiting on another job's step attempt takes the key over once that attempt is gone:
    nothing live holds it any more, so the waiter runs the step itself."""
    global GATE
    GATE = anyio.Event()
    first = await client.jobs.create(roll_once, {"seed": SEED})
    second = await client.jobs.create(roll_once, {"seed": SEED})
    holder = await a_runner(client.database, first, roll_once, roll, owner="holder")
    async with runner_task(holder) as held:
        await wait_until_step_runs(client, first, ROLL_KEY)
        evicted = await current_step_attempt(client, ROLL_KEY)
        assert evicted is not None
        async with runner_task(await a_runner(client.database, second, roll_once, roll)):
            # The second job finds the key pending under a fresh lease, so it waits.
            await wait_until_step_runs(client, second, ROLL_KEY)
            assert (await current_step_attempt(client, ROLL_KEY)) == evicted
            GATE = None  # whatever runs next is not held up
            async with client.database.transaction() as tx:
                job_attempt = await execution.get_job_attempt(tx, holder.job_attempt.id)
                await execution.evict(tx, job_attempt, "stalled")
    assert isinstance(held.error, JobEvictedError)
    async with client.database.transaction() as tx:
        assert (await execution.get_attempt(tx, evicted.id)).state == "abandoned"
    assert (await client.jobs.status(second)).state == "succeeded"
    successor = await current_step_attempt(client, ROLL_KEY)
    assert successor is not None and successor.id != evicted.id
    bound = (await client.jobs.describe(second)).steps[ROLL_KEY].binding
    assert bound is not None and bound.step_result_id == produced_by(successor.id)
    assert len(ROLLS) == 1


async def test_a_hard_cancel_hands_a_shared_step_to_the_other_job(
    client: LocalProjectClient,
) -> None:
    """A hard cancel spares nothing: the step attempt it evicts is abandoned, and the other job
    waiting on the key takes it over rather than waiting for a result that will not come."""
    global GATE
    GATE = anyio.Event()
    first = await client.jobs.create(roll_once, {"seed": SEED})
    second = await client.jobs.create(roll_once, {"seed": SEED})
    cancelled = await a_runner(client.database, first, roll_once, roll, owner="first")
    async with runner_task(cancelled) as one:
        await wait_until_step_runs(client, first, ROLL_KEY)
        async with runner_task(await a_runner(client.database, second, roll_once, roll)):
            await wait_until_step_runs(client, second, ROLL_KEY)
            GATE = None  # whatever runs next is not held up
            await client.jobs.cancel(first, hard=True, reason="stop")
    assert isinstance(one.error, JobEvictedError)
    assert (await client.jobs.status(second)).state == "succeeded"
    assert len(ROLLS) == 1  # the takeover's call is the only one that produced a value
    # The takeover bound every job with an equal request at the key, the cancelled one included,
    # so taking it again finishes it with no new call.
    # Nothing of the evicted job ran to take the result, so it is on offer, not bound: the next
    # job attempt of the first job takes it as it replays the root, without running the step.
    offered = (await client.jobs.describe(first)).steps[ROLL_KEY]
    assert offered.binding is None and isinstance(offered.status, execution.Ready)
    await assert_status_steps_match_describe(client, first)  # on offer, shown stopped
    assert (await run_job(client, first, roll_once, roll)).state == "succeeded"
    assert len(ROLLS) == 1
    assert (await client.jobs.describe(first)).steps[ROLL_KEY].binding is not None
    assert await client.jobs.result(first) == await client.jobs.result(second)


async def test_a_pending_step_wakes_on_its_key_topic(client: LocalProjectClient) -> None:
    """A job waiting on another job's step attempt goes on as soon as that attempt completes: the
    completion wakes the key's topic, so the waiter does not sit out a poll interval."""
    global GATE
    GATE = anyio.Event()
    # Polling and reconciling are set far enough out that only the topic can explain a prompt
    # finish: a waiter that fell back to either would take thirty seconds, not the budget below.
    slow = RunnerSettings(heartbeat_interval=0.05, reconcile_interval=30.0, poll_interval=30.0)
    first = await client.jobs.create(roll_once, {"seed": SEED})
    second = await client.jobs.create(roll_once, {"seed": SEED})
    released = [0.0]

    async def release() -> None:
        await wait_until_step_runs(client, first, ROLL_KEY)
        await wait_until_step_runs(client, second, ROLL_KEY)
        released[0] = anyio.current_time()
        assert GATE is not None
        GATE.set()

    statuses = await run_jobs(
        client, [first, second], roll_once, roll, settings=slow, alongside=release
    )
    assert [s.state for s in statuses] == ["succeeded", "succeeded"]
    assert anyio.current_time() - released[0] < 5.0  # the topic woke it; nothing else could have
    assert len(ROLLS) == 1


# ---- cancelling ----


async def test_a_soft_cancel_lets_a_running_step_finish_and_bind(
    client: LocalProjectClient,
) -> None:
    global GATE
    GATE = anyio.Event()  # the step blocks while the job is cancelled
    job = await client.jobs.create(roll_once, {"seed": SEED})
    runner = await a_runner(client.database, job, roll_once, roll)
    async with runner_task(runner) as outcome:
        await wait_until_step_runs(client, job, ROLL_KEY)
        cancelled = await client.jobs.cancel(job, reason="enough")
        assert cancelled.state == "cancelled" and cancelled.reason == "enough"
        with pytest.raises(NotCancellableError):
            await client.jobs.cancel(job)
        # The root has returned and waits on the step; with no active job attempt it is
        # interrupted, while the step keeps running under the draining runner's fresh lease.
        await wait_until(lambda: invocation_is(client, job, "root", "interrupted"))
        running = await current_step_attempt(client, ROLL_KEY)
        assert running is not None and running.state == "running"
        await assert_status_steps_match_describe(client, job)  # running, job attempt cancelled
        GATE.set()
    assert outcome.settled is not None and outcome.settled.state == "cancelled"
    bound = await current_step_attempt(client, ROLL_KEY)
    assert bound is not None and bound.state == "completed"
    # Resumed, the root replays and finds its request bound: the step ran once.
    assert (await run_job(client, job, roll_once, roll)).state == "succeeded"
    assert len(ROLLS) == 1
    assert [ja.state for ja in (await client.jobs.status(job)).job_attempts] == [
        "cancelled",
        "succeeded",
    ]


async def test_a_runner_started_on_a_cancelled_job_attempt_drains_at_once(
    client: LocalProjectClient,
) -> None:
    """A runner whose job attempt is cancelled before it runs starts nothing: the queue it derives
    is empty, every start is refused, and it returns the cancelled job attempt at once."""
    job = await client.jobs.create(reshape, {"rows": ROWS})
    runner = await a_runner(client.database, job, reshape)
    cancelled = await client.jobs.cancel(job)
    assert cancelled.state == "cancelled"
    status = await client.jobs.status(job)
    # The job attempt is cancelled, so nothing is due: the root is idle with nothing to explain it.
    assert status.state == "stopped" and status.invocations["root"].state == "stopped"
    settled = await runner.run()
    assert settled.state == "cancelled" and settled.id == cancelled.id
    assert not (await client.jobs.describe(job)).invocations["root"].attempts  # nothing started
    # Taking the job again starts the next job attempt, which runs it.
    assert (await run_job(client, job, reshape)).state == "succeeded"
    (only,) = (await client.jobs.describe(job)).invocations["root"].attempts
    assert only.attempt.state == "completed"


async def test_a_hard_cancel_stops_the_running_body(client: LocalProjectClient) -> None:
    global GATE
    GATE = anyio.Event()  # never opened: the body blocks after observing
    job = await client.jobs.create(two_phase, {"rows": ROWS})
    runner = await a_runner(client.database, job, two_phase)
    async with runner_task(runner) as outcome:
        await wait_until_running(client, job)
        await client.jobs.cancel(job, hard=True, reason="stop")
    # The hard cancel evicted the job attempt: its runner lost the job and stopped, and the
    # attempt it was running was abandoned by the evictor.
    assert isinstance(outcome.error, JobEvictedError)
    status = await client.jobs.status(job)
    assert status.state == "stopped"
    assert status.job_attempt.state == "cancelled" and status.job_attempt.evicted
    GATE = None
    assert (await run_job(client, job, two_phase)).state == "succeeded"
    first, second = (await client.jobs.describe(job)).invocations["root"].attempts
    assert first.attempt.state == "abandoned" and first.attempt.reason == "cancelled: stop"
    assert second.attempt.state == "completed"


async def test_a_hard_cancel_during_a_verification_run_stops_it(client: LocalProjectClient) -> None:
    global GATE
    GATE = anyio.Event()
    ON_SECOND_RUN[0] = "block"
    job = await client.jobs.create(counted, {"rows": ROWS})
    runner = await a_runner(client.database, job, counted, verify_determinism=True)
    async with runner_task(runner) as outcome:
        await wait_until_verifying()
        await client.jobs.cancel(job, hard=True, reason="stop")
    assert isinstance(outcome.error, JobEvictedError)
    GATE = None
    ON_SECOND_RUN[0] = "nothing"
    BODY_CALLS[0] = 0
    assert (await run_job(client, job, counted)).state == "succeeded"
    first, second = (await client.jobs.describe(job)).invocations["root"].attempts
    assert first.attempt.state == "abandoned" and second.attempt.state == "completed"


async def test_an_evicted_runner_stops_and_its_late_writes_are_refused(
    client: LocalProjectClient,
) -> None:
    global GATE
    GATE = anyio.Event()
    job = await client.jobs.create(two_phase, {"rows": ROWS})
    runner = await a_runner(client.database, job, two_phase)
    async with runner_task(runner) as outcome:
        await wait_until_running(client, job)
        async with client.database.transaction() as tx:
            job_attempt = await execution.get_job_attempt(tx, runner.job_attempt.id)
            await execution.evict(tx, job_attempt, "stalled")
    assert isinstance(outcome.error, JobEvictedError)
    described = await client.jobs.describe(job)
    (attempt,) = described.invocations["root"].attempts
    assert attempt.attempt.state == "abandoned" and attempt.attempt.reason == "stalled"
    assert described.job_attempts[-1].evicted and described.job_attempts[-1].state == "stopped"
    # Nothing the evicted runner still held may write: the guard refuses its attempt.
    with pytest.raises(StaleAttemptError):
        async with client.database.transaction() as tx:
            await execution.guard(tx, attempt.attempt.id)
    GATE = None
    assert (await run_job(client, job, two_phase)).state == "succeeded"


async def test_ctrl_c_is_soft_then_hard(client: LocalProjectClient) -> None:
    """The two levels of Ctrl-C: the first interrupt settles the job attempt cancelled and drains,
    so a step already running finishes and binds; the second stops what is still running, which is
    abandoned. Either way the job attempt is settled, so taking the job again needs no takeover."""
    global GATE, SECOND_GATE
    GATE = anyio.Event()  # the first step, released after the first interrupt
    SECOND_GATE = anyio.Event()  # the second step, never released
    job = await client.jobs.create(two_rolls, {"seed": SEED})
    runner = await a_runner(client.database, job, two_rolls, roll, second_roll)
    async with runner_task(runner) as outcome:
        await wait_until_step_runs(client, job, "root/one")
        await wait_until_step_runs(client, job, "root/two")
        runner.interrupt()  # the first press: drain
        await wait_until(lambda: job_attempt_is(client, job, "cancelled"))
        GATE.set()  # the draining runner lets the running step finish

        async def one_bound() -> bool:
            attempt = await current_step_attempt(client, "root/one")
            return attempt is not None and attempt.state == "completed"

        await wait_until(one_bound)
        runner.interrupt()  # the second press: stop what is still running
    assert outcome.settled is not None and outcome.settled.state == "cancelled"
    assert not outcome.settled.evicted  # the runner stopped itself; nothing evicted it
    one = await current_step_attempt(client, "root/one")
    two = await current_step_attempt(client, "root/two")
    assert one is not None and one.state == "completed"
    assert two is not None and two.state == "abandoned"
    # The finished step's result is there for the root to take, and the second interrupt stops the
    # root. Which of the two happens first is not decided, and the job resumes either way.
    offered = (await client.jobs.describe(job)).steps["root/one"]
    if offered.binding is None:
        assert offered.status == execution.Ready(
            produced_by(one.id)
        )  # stopped before it could take it
    else:
        assert offered.binding.step_result_id == produced_by(
            one.id
        )  # it got there first, which is equally fine
    # Taking the job again needs no takeover: it takes that result and runs the other step.
    SECOND_GATE = None
    assert (await run_job(client, job, two_rolls, roll, second_roll)).state == "succeeded"
    assert len(ROLLS) == 2  # the finished step was not run again
    binding = (await client.jobs.describe(job)).steps["root/one"].binding
    assert binding is not None and binding.step_result_id == produced_by(one.id)
    assert [ja.state for ja in (await client.jobs.status(job)).job_attempts] == [
        "cancelled",
        "succeeded",
    ]


# ---- limits ----


@step(inputs={"x": int}, output=int)
async def double(context: StepContext, x: int) -> int:
    STEPS_RUNNING[0] += 1
    STEPS_AT_ONCE[0] = max(STEPS_AT_ONCE[0], STEPS_RUNNING[0])
    try:
        await anyio.sleep(0.05)
        return 2 * x
    finally:
        STEPS_RUNNING[0] -= 1


@workflow(inputs={"x": int}, output=int)
async def double_and_look(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    """A child whose body blocks in ``observe`` until its step has run."""
    BODIES_RUNNING[0] += 1
    BODIES_AT_ONCE[0] = max(BODIES_AT_ONCE[0], BODIES_RUNNING[0])
    try:
        out = context.run_step("double", double, {"x": x})
        await out.observe()
        return out
    finally:
        BODIES_RUNNING[0] -= 1


@workflow(inputs={"ids": IDS_TYPE}, output=IDS_TYPE)
async def double_each_child(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_child_workflow("d", double_and_look, {"x": ids}, map_over=["id"])


async def test_workflow_bodies_are_not_limited(client: LocalProjectClient) -> None:
    """Only steps are limited. Four child bodies block in ``observe`` at once while a single step
    slot runs their steps one after another, and the job completes."""
    job = await client.jobs.create(double_each_child, {"ids": FOUR_IDS})
    status = await run_job(
        client,
        job,
        double_each_child,
        double_and_look,
        double,
        verify_determinism=False,
        limits=RunLimits(steps=1),
    )
    assert status.state == "succeeded"
    assert STEPS_AT_ONCE[0] == 1
    assert BODIES_AT_ONCE[0] == 4
    assert await client.jobs.result(job) == Array.from_items(
        [((i,), 20 * i) for i in (1, 2, 3, 4)], IDS_TYPE
    )


# ---- logging ----


@step(inputs={"x": int}, output=int)
async def logging_step(context: StepContext, x: int) -> int:
    context.log(f"step doubling {x}")
    return 2 * x


@workflow(inputs={"x": int}, output=int)
async def logging_root(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    context.log("root started")
    out = context.run_step("double", logging_step, {"x": x})
    context.log("root scheduled the step")
    return out


async def test_ctx_log_persists_per_attempt_for_workflow_and_step(
    client: LocalProjectClient,
) -> None:
    job = await client.jobs.create(logging_root, {"x": Array.scalar(3, int)})
    assert (await run_job(client, job, logging_root, logging_step)).state == "succeeded"
    assert await client.jobs.result(job) == Array.scalar(6, int)
    described = await client.jobs.describe(job)
    (root_attempt,) = described.invocations["root"].attempts
    # The workflow's lines, written from the recorder's batch transactions, in body order.
    assert [log.message for log in root_attempt.logs] == ["root started", "root scheduled the step"]
    assert [log.seq for log in root_attempt.logs] == [0, 1]
    # The step's line, written by its background flush task.
    step_desc = described.steps["root/double"]
    assert [log.message for log in step_desc.logs] == ["step doubling 3"]


@workflow(inputs={"rows": ROWS_TYPE}, output=("[k: int]", int))
async def logging_two_phase(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[int]:
    context.log("attempt running")
    x = rows.field("x").collect()
    await x.observe()  # forces the batch (with the log) to commit before the gate blocks
    await gate_open()
    return x.rename({"id": "k"}).collect()


async def test_logs_are_per_attempt_and_not_deduped_across_a_replay(
    client: LocalProjectClient,
) -> None:
    global GATE
    GATE = anyio.Event()  # never opened: the first attempt blocks after logging and observing
    job = await client.jobs.create(logging_two_phase, {"rows": ROWS})
    runner = await a_runner(client.database, job, logging_two_phase)
    async with runner_task(runner) as outcome:
        await wait_until_recorded(client, job, nodes.Query)
        outcome.kill()
    GATE = None
    assert (await run_job(client, job, logging_two_phase)).state == "succeeded"
    first, second = (await client.jobs.describe(job)).invocations["root"].attempts
    assert first.attempt.state == "abandoned" and second.attempt.state == "completed"
    # Each attempt wrote its own copy of the line under its own id: not deduped across the replay.
    assert [log.message for log in first.logs] == ["attempt running"]
    assert [log.message for log in second.logs] == ["attempt running"]
    assert first.attempt.id != second.attempt.id


@step(inputs={"seed": str}, output=int)
async def logging_then_raising_step(context: StepContext, seed: str) -> int:
    context.log("about to fail")
    raise RuntimeError("step boom")


@workflow(inputs={"seed": str}, output=int)
async def roll_logging_failure(
    context: WorkflowContext, seed: ArrayHandle[str]
) -> ArrayHandle[int]:
    return context.run_step("roll", logging_then_raising_step, {"seed": seed})


async def test_a_caught_step_exception_still_persists_its_buffered_logs(
    client: LocalProjectClient,
) -> None:
    job = await client.jobs.create(roll_logging_failure, {"seed": SEED})
    assert (await run_job(client, job, roll_logging_failure, logging_then_raising_step)).state == (
        "stopped"
    )
    step_desc = (await client.jobs.describe(job)).steps[ROLL_KEY]
    assert step_desc.attempt is not None and step_desc.attempt.state == "failed"
    # The line the step logged before raising was finalized on the fail-close, so it survives.
    assert [log.message for log in step_desc.logs] == ["about to fail"]
