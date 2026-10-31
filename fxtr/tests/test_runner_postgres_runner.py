"""The job runner in isolation over a real database: seeding and pushing work, starting each item
once, settling, the lease (an eviction and a cancel from elsewhere), interrupting, the step
limiter, reconciliation taking over a step whose owner went stale, and the array queries a job
runs — on the engine, and again on a second attempt and in a later job."""

from __future__ import annotations

import contextvars
from collections.abc import Mapping, Sequence
from typing import Any, cast

import anyio
import pytest
from postgres_testing import (
    FAST,
    a_code_version,
    a_job,
    a_runner,
    active,
    age_heartbeat,
    behind,
    request,
    runner_task,
    running_root,
    set_attempt_state,
    start_root,
    start_step,
    wait_until,
)

import fxtr
from fxtr.core import codec
from fxtr.core.entity_data import EntityID
from fxtr.duckdb_engine import local_query
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.query import count, value
from fxtr.entity_defns.query_plan import QueryPlan
from fxtr.entity_defns.schema import IdSchema, optional, struct_of
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.registry import FunctionRegistry
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.runner import nodes
from fxtr.runner.driver import in_process_executor
from fxtr.runner.steps import StepRequest

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres import execution, ledger  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.entities import PostgresEntityStorage  # noqa: E402
from fxtr.runner.postgres.execution import (  # noqa: E402
    AttemptRecord,
    StartInvocation,
)
from fxtr.runner.postgres.jobs import Jobs  # noqa: E402
from fxtr.runner.postgres.ledger import InvocationRecord, JobID  # noqa: E402
from fxtr.runner.postgres.runner import (  # noqa: E402
    UNREADABLE_REQUEST,
    JobEvictedError,
    JobRunner,
    RunLimits,
    RunnerSettings,
    run_job,
)

pytestmark = pytest.mark.anyio

IDS_TYPE = ArrayType.of(("[id: int]", int))
IDS = Array.from_items([((1,), 10), ((2,), 20)], IDS_TYPE)
OTHER_IDS = Array.from_items([((1,), 30), ((2,), 40)], IDS_TYPE)

# Knobs the test bodies read: a gate that holds a body or a step open, and counters.
GATE: anyio.Event | None = None
BODY_CALLS = [0]
STEP_CALLS = [0]
STEPS_RUNNING = [0]
STEPS_AT_ONCE = [0]


@pytest.fixture(autouse=True)
def reset_knobs() -> None:
    global GATE
    GATE = None
    BODY_CALLS[0] = 0
    STEP_CALLS[0] = 0
    STEPS_RUNNING[0] = 0
    STEPS_AT_ONCE[0] = 0


async def gate_open() -> None:
    if GATE is not None:
        await GATE.wait()


# ---- the functions the tests run ----


@workflow(inputs={"x": int}, output=int)
async def echo(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    BODY_CALLS[0] += 1
    return x


@workflow(inputs={"x": int}, output=int)
async def gated(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    BODY_CALLS[0] += 1
    await x.observe()
    await gate_open()
    return x


@workflow(inputs={"x": int}, output=("[copy: str]", int))
async def child(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.stack({"a": x, "b": x}, dim="copy")


@workflow(inputs={"ids": IDS_TYPE}, output=("[id: int, copy: str]", int))
async def parent(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_child_workflow("child", child, {"x": ids}, map_over=["id"])


@step(inputs={"x": int}, output=int)
async def double(context: StepContext, x: int) -> int:
    STEP_CALLS[0] += 1
    STEPS_RUNNING[0] += 1
    STEPS_AT_ONCE[0] = max(STEPS_AT_ONCE[0], STEPS_RUNNING[0])
    try:
        await anyio.sleep(0.05)  # long enough for a second step to overlap, if one may
        await gate_open()
        return 2 * x
    finally:
        STEPS_RUNNING[0] -= 1


@workflow(inputs={"x": int}, output=int)
async def double_once(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("double", double, {"x": x})


@workflow(inputs={"ids": IDS_TYPE}, output=IDS_TYPE)
async def double_each(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("double", double, {"x": ids}, map_over=["id"])


# ---- helpers ----


def jobs(database: Database) -> Jobs:
    return Jobs(database, PostgresEntityStorage(database))


async def run(database: Database, job: JobID, registry: FunctionRegistry, **kwargs: Any) -> Any:
    """``run_job`` at the fixture code version."""
    code_version = await a_code_version(database)
    return await run_job(database, job, registry, code_version=code_version, **kwargs)


async def root_of(database: Database, job_id: JobID) -> InvocationRecord:
    async with database.transaction() as tx:
        return await ledger.find_invocation(tx, job_id, "root")


async def attempts_of(database: Database, job_id: JobID) -> list[AttemptRecord]:
    """The attempts of the job's root invocation, oldest first."""
    root = await root_of(database, job_id)
    async with database.transaction() as tx:
        return await execution.list_attempts(tx, root.id)


async def step_attempts(database: Database) -> list[AttemptRecord]:
    """The attempt behind every cache entry: its current one, or the one that produced the step
    result it offers (the step result's id)."""
    async with database.transaction() as tx:
        entries = await execution.list_cache_entries(tx)
        return [await execution.get_attempt(tx, behind(entry)) for entry in entries]


async def explain(database: Database, job_id: JobID) -> str:
    """Every invocation's attempts with their states, errors and reasons: what to read when a
    job did not end the way a test expected."""
    described = await jobs(database).describe(job_id)
    return repr(
        {
            path: [(a.attempt.state, a.attempt.error, a.attempt.reason) for a in inv.attempts]
            for path, inv in described.invocations.items()
        }
    )


async def wait_until_running(database: Database, job_id: JobID) -> None:
    async def running() -> bool:
        return any(a.live for a in await attempts_of(database, job_id))

    await wait_until(running)


async def in_the_body() -> bool:
    return BODY_CALLS[0] == 1


async def wait_until_a_step_runs(database: Database) -> None:
    async def running() -> bool:
        return any(a.live for a in await step_attempts(database))

    await wait_until(running)


# ---- seeding, pushing, and settling ----


async def test_a_runner_seeds_its_queue_from_pending_work_and_settles_succeeded(
    database: Database,
) -> None:
    job = await jobs(database).create(echo, {"x": Array.scalar(2, int)})
    settled = await run(database, job, FunctionRegistry([echo]), settings=FAST)
    assert settled.state == "succeeded"
    assert await jobs(database).result(job) == Array.scalar(2, int)


STAGGERED_IDS = Array.from_items([((i,), i) for i in range(1, 7)], IDS_TYPE)


@step(inputs={"x": int}, output=int)
async def staggered(context: StepContext, x: int) -> int:
    await anyio.sleep(0.03 * x)  # the steps finish one after another
    return x


@workflow(inputs={"ids": IDS_TYPE}, output=IDS_TYPE)
async def staggered_each(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("staggered", staggered, {"x": ids}, map_over=["id"])


async def test_collection_does_not_judge_a_target_again_once_done(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once a pass finds a step done, later passes leave it out of the keys they judge: a pass
    costs what is outstanding, so a node of many steps does not re-judge its finished ones on
    every wake."""
    job = await jobs(database).create(staggered_each, {"ids": STAGGERED_IDS})
    runner = await a_runner(
        database, job, staggered_each, staggered, limits=RunLimits(steps=len(STAGGERED_IDS))
    )
    step_statuses = execution.step_statuses
    done: set[str] = set()
    judged_again: set[str] = set()
    passes_after_a_done_step = 0

    async def watched(tx: Any, job_id: JobID, cache_keys: Any) -> dict[str, Any]:
        nonlocal passes_after_a_done_step
        keys = set(cache_keys)
        judged_again.update(keys & done)
        passes_after_a_done_step += bool(done)
        statuses = await step_statuses(tx, job_id, keys)
        # A step this job ran is on offer (Ready) when first seen finished, and the pass binds it
        # at once; nothing clears a key here, so either way it is done from then on.
        done.update(
            key
            for key, status in statuses.items()
            if isinstance(status, execution.Done | execution.Ready)
        )
        return statuses

    monkeypatch.setattr(execution, "step_statuses", watched)
    assert (await runner.run()).state == "succeeded"
    assert await jobs(database).result(job) == STAGGERED_IDS
    assert len(done) == len(STAGGERED_IDS) and passes_after_a_done_step > 0
    assert judged_again == set()


async def test_step_completions_progress_without_reconciling_a_live_workflow(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steps ending while their workflow is live trigger no settle: the evaluator's pushes carry the
    job forward, and the runner settles only in its first pass (the seed) and once the root's
    ending leaves nothing live. Each settle reads the whole job while holding the job attempt's
    row."""
    collected = anyio.Event()
    release = anyio.Event()

    @workflow(name="test.gated_collection", inputs={"ids": IDS_TYPE}, output=IDS_TYPE)
    async def gated_collection(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
        result = context.run_step("double", double, {"x": ids}, map_over=["id"])
        await result.observe()
        collected.set()
        await release.wait()
        return result

    job = await jobs(database).create(gated_collection, {"ids": IDS})
    runner = await a_runner(
        database, job, gated_collection, double, settings=RunnerSettings(reconcile_interval=60)
    )
    settle = execution.settle
    passes = 0

    async def counted(tx: Any, job_id: JobID) -> execution.Settlement:
        nonlocal passes
        passes += 1
        return await settle(tx, job_id)

    monkeypatch.setattr(execution, "settle", counted)
    async with runner_task(runner) as outcome:
        await collected.wait()
        # The steps have all ended while the workflow is live: none of their endings settled.
        await anyio.sleep(0.05)
        assert passes == 1  # the seed
        release.set()
    assert outcome.error is None
    assert outcome.settled is not None and outcome.settled.state == "succeeded"
    assert passes == 2  # the root's ending left nothing live


async def test_periodic_reconciliation_recovers_dropped_pushes_with_a_live_parent(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step whose push never reached the queue still runs while its workflow is live: the
    reconciliation interval re-derives what is due, without waiting for the runner to be idle."""
    job = await jobs(database).create(double_each, {"ids": IDS})
    runner = await a_runner(database, job, double_each, double, settings=FAST)
    reconcile = runner._reconcile
    enqueue = runner.enqueue
    # Set only in the scheduling task, for the length of a pass: item tasks keep pushing while a
    # pass waits on the database, and those pushes must still be dropped.
    reconciling = contextvars.ContextVar("reconciling", default=False)
    dropped = 0

    async def counted() -> execution.JobAttemptRecord | None:
        token = reconciling.set(True)
        try:
            return await reconcile()
        finally:
            reconciling.reset(token)

    def drop_push(item: execution.WorkItem) -> None:
        nonlocal dropped
        if isinstance(item, execution.StartStep) and not reconciling.get():
            dropped += 1
            return
        enqueue(item)

    monkeypatch.setattr(runner, "_reconcile", counted)
    monkeypatch.setattr(runner, "enqueue", drop_push)
    assert (await runner.run()).state == "succeeded"
    assert dropped > 0
    assert STEP_CALLS[0] == len(IDS)


async def test_two_pushes_for_one_invocation_start_one_attempt(database: Database) -> None:
    job = await jobs(database).create(echo, {"x": Array.scalar(2, int)})
    runner = await a_runner(database, job, echo)
    root = await root_of(database, job)
    runner.enqueue(StartInvocation(root.id))
    runner.enqueue(StartInvocation(root.id))  # the same item, and the seed names it as well
    settled = await runner.run()
    assert settled.state == "succeeded"
    assert BODY_CALLS[0] == 1
    assert [a.state for a in await attempts_of(database, job)] == ["completed"]


async def test_the_children_an_expansion_creates_are_pushed_and_run(database: Database) -> None:
    job = await jobs(database).create(parent, {"ids": IDS})
    settled = await run(database, job, FunctionRegistry([parent, child]), settings=FAST)
    assert settled.state == "succeeded", await explain(database, job)
    status = await jobs(database).status(job)
    assert {path: s.state for path, s in status.invocations.items()} == {
        "root": "completed",
        "root/child[id:1]": "completed",
        "root/child[id:2]": "completed",
    }


async def test_a_runner_that_starts_nothing_still_settles_stopped(database: Database) -> None:
    # The root was tried once under this job attempt and failed, staged without the close hook, so
    # nothing is due, nothing runs, and no attempt will close: only the runner's own settle can
    # end the episode.
    job = await a_job(database)
    started = await start_root(database, job)
    await set_attempt_state(database, started.attempt_id, "failed")
    job_attempt = await active(database, job)
    runner = JobRunner(
        database, job_attempt, execute=in_process_executor(FunctionRegistry()), settings=FAST
    )
    assert (await runner.run()).state == "stopped"


async def test_a_job_whose_only_work_failed_settles_stopped(database: Database) -> None:
    job = await jobs(database).create(echo, {"x": Array.scalar(2, int)})
    settled = await run(database, job, FunctionRegistry(), settings=FAST)  # no such workflow
    assert settled.state == "stopped"
    assert [a.state for a in await attempts_of(database, job)] == ["failed"]


# ---- the running row is covered from the moment it exists ----


async def test_a_workflow_task_cancelled_while_its_body_runs_still_abandons_the_row(
    database: Database,
) -> None:
    """The attempt object is built before the first await, so the ``finally`` that abandons the
    row covers the whole body. A cancellation anywhere in it leaves the row abandoned, not
    running."""
    global GATE
    GATE = anyio.Event()  # never opened: the body blocks until the task is cancelled
    job = await jobs(database).create(gated, {"x": Array.scalar(2, int)})
    runner = await a_runner(database, job, gated)
    async with runner_task(runner) as outcome:
        await wait_until_running(database, job)  # the row exists
        await wait_until(in_the_body)  # and the task is inside it, held at the gate
        outcome.kill()
    assert outcome.settled is None  # cancelled from outside, so it returned nothing
    assert [a.state for a in await attempts_of(database, job)] == ["abandoned"]
    async with database.transaction() as tx:
        (job_attempt,) = await execution.list_job_attempts(tx, job)
    assert job_attempt.state == "cancelled"


async def test_a_step_task_cancelled_while_loading_its_request_still_abandons_the_row(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = anyio.Event()
    original = PostgresEntityStorage.load

    async def slow_load(self: PostgresEntityStorage, ref: Any, **kwargs: Any) -> Any:
        loader = getattr(ref, "loader", None)
        if loader is not None and loader.static_type_id == StepRequest.fxtr_type_id():
            await gate.wait()  # the step attempt's request load, never released
        return await original(self, ref, **kwargs)

    monkeypatch.setattr(PostgresEntityStorage, "load", slow_load)
    job = await jobs(database).create(double_once, {"x": Array.scalar(2, int)})
    runner = await a_runner(database, job, double_once, double)
    async with runner_task(runner) as outcome:
        await wait_until_a_step_runs(database)  # the step's row exists; its task is in the load
        outcome.kill()
    assert outcome.settled is None  # cancelled from outside, so it returned nothing
    assert [a.state for a in await step_attempts(database)] == ["abandoned"]
    assert STEP_CALLS[0] == 0  # the step never ran


# ---- the lease ----


async def test_the_heartbeat_notices_an_eviction_and_the_runner_stops(database: Database) -> None:
    global GATE
    GATE = anyio.Event()
    job = await jobs(database).create(gated, {"x": Array.scalar(2, int)})
    runner = await a_runner(database, job, gated)
    async with runner_task(runner) as outcome:
        await wait_until_running(database, job)
        async with database.transaction() as tx:
            job_attempt = await execution.get_job_attempt(tx, runner.job_attempt.id)
            await execution.evict(tx, job_attempt, "taken over")
    assert isinstance(outcome.error, JobEvictedError)
    async with database.transaction() as tx:
        job_attempt = await execution.get_job_attempt(tx, runner.job_attempt.id)
    assert job_attempt.evicted and job_attempt.state == "stopped"  # the evictor settled it
    assert [a.state for a in await attempts_of(database, job)] == ["abandoned"]


async def test_a_cancel_from_elsewhere_drains_and_lets_a_running_step_finish(
    database: Database,
) -> None:
    global GATE
    GATE = anyio.Event()
    job = await jobs(database).create(double_once, {"x": Array.scalar(2, int)})
    runner = await a_runner(database, job, double_once, double)
    async with runner_task(runner) as outcome:
        await wait_until_a_step_runs(database)
        await jobs(database).cancel(job)
        await anyio.sleep(0.2)  # several heartbeats: the runner knows, the step runs on
        assert [a.state for a in await step_attempts(database)] == ["running"]
        GATE.set()
    assert outcome.settled is not None and outcome.settled.state == "cancelled"
    assert [a.state for a in await step_attempts(database)] == ["completed"]
    # The drained step's result is on offer at the key; nothing of the cancelled job ran to take
    # it, so the job binds to it when it is next run, without running the step again.
    async with database.transaction() as tx:
        entry = (await execution.list_cache_entries(tx))[0]
        assert await ledger.get_step_binding(tx, job, entry.cache_key) is None
        assert isinstance(await execution.step_status(tx, job, entry.cache_key), execution.Ready)
    settled = await (await a_runner(database, job, double_once, double)).run()
    assert settled.state == "succeeded" and STEP_CALLS[0] == 1
    async with database.transaction() as tx:
        assert await ledger.get_step_binding(tx, job, entry.cache_key) is not None


# ---- interrupting ----


async def test_an_interrupt_settles_cancelled_and_lets_a_running_step_finish(
    database: Database,
) -> None:
    global GATE
    GATE = anyio.Event()
    job = await jobs(database).create(double_once, {"x": Array.scalar(2, int)})
    runner = await a_runner(database, job, double_once, double)
    async with runner_task(runner) as outcome:
        await wait_until_a_step_runs(database)
        runner.interrupt()
        await anyio.sleep(0.2)
        assert [a.state for a in await step_attempts(database)] == ["running"]
        GATE.set()
    assert outcome.settled is not None and outcome.settled.state == "cancelled"
    assert [a.state for a in await step_attempts(database)] == ["completed"]


async def test_a_second_interrupt_abandons_what_is_still_running(database: Database) -> None:
    global GATE
    GATE = anyio.Event()  # never opened: the step only ends by being stopped
    job = await jobs(database).create(double_once, {"x": Array.scalar(2, int)})
    runner = await a_runner(database, job, double_once, double)
    async with runner_task(runner) as outcome:
        await wait_until_a_step_runs(database)
        runner.interrupt()
        await anyio.sleep(0.1)
        runner.interrupt()
    assert outcome.settled is not None and outcome.settled.state == "cancelled"
    assert [a.state for a in await step_attempts(database)] == ["abandoned"]
    # The body stopped too: interrupted, if it noticed that nothing of the job is pending any more
    # before the second interrupt reached it, and abandoned otherwise.
    (root_attempt,) = await attempts_of(database, job)
    assert root_attempt.state in ("interrupted", "abandoned")


# ---- limits ----


async def test_the_step_limiter_bounds_concurrent_steps(database: Database) -> None:
    one = await jobs(database).create(double_each, {"ids": IDS})
    settled = await run(
        database,
        one,
        FunctionRegistry([double_each, double]),
        settings=FAST,
        limits=RunLimits(steps=1),
    )
    assert settled.state == "succeeded"
    assert STEP_CALLS[0] == 2 and STEPS_AT_ONCE[0] == 1
    # The same workflow again with room for both. Its root has another pathname, so its steps get
    # other cache keys and run rather than replay.
    STEPS_AT_ONCE[0] = 0
    two = await jobs(database).create(double_each, {"ids": OTHER_IDS}, root="again")
    settled = await run(
        database,
        two,
        FunctionRegistry([double_each, double]),
        settings=FAST,
        limits=RunLimits(steps=2),
    )
    assert settled.state == "succeeded"
    assert STEP_CALLS[0] == 4 and STEPS_AT_ONCE[0] == 2


# ---- reconciliation ----


async def test_a_step_held_by_an_evicted_job_attempt_is_taken_over(
    database: Database,
) -> None:
    inputs = {"x": Array.scalar(2, int)}
    functions = FunctionRegistry([double_once, double])
    # Run one job to learn the cache key its step is saved under, then free the key again.
    first = await jobs(database).create(double_once, inputs)
    assert (await run(database, first, functions, settings=FAST)).state == "succeeded"
    async with database.transaction() as tx:
        key = (await execution.list_cache_entries(tx))[0].cache_key
        recorded = await ledger.get_job_step_request(tx, first, key)
        assert recorded is not None
        await execution.clear_cache_entry(tx, key)
    # A third job holds the key with an equal request, under a lease that is fresh for now.
    holder, holder_attempt = await running_root(database, owner="holder")
    await request(database, holder, holder_attempt, key, recorded.request.bind_type(StepRequest))
    assert await start_step(database, holder, key) is not None
    # The job under test therefore finds the key pending, and nothing pushes it.
    second = await jobs(database).create(double_once, inputs)
    runner = await a_runner(database, second, double_once, double)
    async with runner_task(runner) as outcome:

        async def requested() -> bool:
            async with database.transaction() as tx:
                return await ledger.get_job_step_request(tx, second, key) is not None

        await wait_until(requested)
        assert STEP_CALLS[0] == 1  # nobody is running it for this job
        await age_heartbeat(database, (await active(database, holder)).id, 120)
        await anyio.sleep(3 * FAST.reconcile_interval)
        assert STEP_CALLS[0] == 1  # a stale lease alone changes nothing: the job keeps waiting
        async with database.transaction() as tx:
            await execution.evict(tx, await active(database, holder), "gone")
    assert outcome.settled is not None and outcome.settled.state == "succeeded"
    assert STEP_CALLS[0] == 2  # the eviction freed the key; this job's runner took it over


QUERY_ROWS_TYPE = ArrayType.of(("[id: int]", struct_of(x=int, y=str)))
QUERY_ROWS = Array.from_records(
    [{"id": 1, "x": 10, "y": "p"}, {"id": 2, "x": 20, "y": "q"}], QUERY_ROWS_TYPE
)
QUERY_CHAIN_TYPE = ArrayType.of(("[y: str]", struct_of(n=int, largest=optional(int))))


QUERY_CHAIN_RESULT = Array.from_items([(("q",), {"n": 1, "largest": 20})], QUERY_CHAIN_TYPE)


def chain_of(rows: ArrayHandle[Any]) -> ArrayHandle[Any]:
    """A filter two aggregates read, joined by SQL: the filter is collected into a node of its
    own, and the aggregates are part of the SQL node that reads it twice."""
    kept = rows.query().filter(value().field("x") >= 15).collect()
    by_y = {"y": value().field("y")}
    counts = kept.query().aggregate({"n": count()}, group_by=by_y)
    largest = kept.query().aggregate({"largest": value().field("x").max()}, group_by=by_y)
    return fxtr.sql(
        "SELECT counts.dims, {n: counts.value.n, largest: largest.value.largest} AS value"
        " FROM counts JOIN largest USING (dims)",
        tables={"counts": counts, "largest": largest},
        output_type=QUERY_CHAIN_TYPE,
    ).collect()


@workflow(inputs={"rows": QUERY_ROWS_TYPE}, output=QUERY_CHAIN_TYPE)
async def query_chain(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[Any]:
    return chain_of(rows)


@workflow(inputs={"rows": QUERY_ROWS_TYPE}, output=QUERY_CHAIN_TYPE)
async def gated_query_chain(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[Any]:
    """The chain held open once its queries have answered, so an attempt can be killed after
    running them."""
    BODY_CALLS[0] += 1
    joined = chain_of(rows)
    await joined.observe()  # both query nodes have run by the time this returns
    await gate_open()
    return joined


async def test_a_workflows_queries_run_on_the_engine_and_store_nothing_of_their_own(
    database: Database,
) -> None:
    """A query node is computed like a derived node: its value is not stored for its own sake."""
    job = await jobs(database).create(query_chain, {"rows": QUERY_ROWS})
    settled = await run(database, job, FunctionRegistry([query_chain]), settings=FAST)
    assert settled.state == "succeeded"
    assert await jobs(database).result(job) == QUERY_CHAIN_RESULT
    root = (await jobs(database).describe(job)).invocations["root"]
    assert [nodes.kind_of(n.node) for n in root.nodes if n.value is not None] == ["input"]
    queries = {n.identity: n.node for n in root.nodes if isinstance(n.node, nodes.Query)}
    (sql,) = [query for query in queries.values() if len(query.inputs) == 2]
    (filtered,) = [identity for identity, query in queries.items() if query is not sql]
    assert sql.inputs == (filtered, filtered)  # one filter node, read twice


UNSTORED = EntityID(cid=codec.compute_cid(codec.encode({"$type": "tests.nowhere", "n": 1})))
"""An entity ID no storage holds."""
IDS_OUT = ArrayType.of(("[id: int]", IdSchema()))


def unstored_ids(ids: ArrayHandle[Any]) -> ArrayHandle[Any]:
    """A query that writes an entity ID into every row: any CID, as Python could build."""
    return fxtr.sql(
        "SELECT dims, $ref AS value FROM ids",
        tables={"ids": ids},
        output_type=IDS_OUT,
        parameters={"ref": UNSTORED},
    ).collect()


@step(name="tests.count_ids", inputs={"refs": IDS_OUT}, output=int)
async def count_ids(context: StepContext, refs: Array[Any]) -> int:
    return len(refs)


@workflow(inputs={"ids": IDS_TYPE}, output=int)
async def unstored_to_a_step(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("count", count_ids, {"refs": unstored_ids(ids)})


@workflow(inputs={"ids": IDS_TYPE}, output=IDS_OUT)
async def unstored_returned(context: WorkflowContext, ids: ArrayHandle[int]) -> ArrayHandle[Any]:
    return unstored_ids(ids)


@pytest.mark.parametrize("root", [unstored_to_a_step, unstored_returned])
async def test_a_query_naming_an_unstored_entity_fails_the_attempt(
    database: Database, root: Any
) -> None:
    """Storing a query result that names an entity storage lacks, as a step's input or as the
    return, fails the attempt with that refusal; the runner settles rather than raising."""
    job = await jobs(database).create(root, {"ids": IDS})
    settled = await run(database, job, FunctionRegistry([root, count_ids]), settings=FAST)
    assert settled.state == "stopped"
    (attempt,) = await attempts_of(database, job)
    assert attempt.state == "failed"
    assert attempt.error is not None and attempt.error.type_name == "UnknownReferenceError"
    assert STEP_CALLS[0] == 0


@step(name="tests.total", inputs={"counted": QUERY_CHAIN_TYPE}, output=int)
async def total_of(context: StepContext, counted: Array[Any]) -> int:
    """A step reads a query's rows, so the query's result has to become Python values."""
    rows = cast("list[Mapping[str, Any]]", list(counted.raw_values()))
    return sum(int(cast("int", row["n"])) for row in rows)


@workflow(inputs={"rows": QUERY_ROWS_TYPE}, output=int)
async def query_then_step(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[int]:
    """A query node feeding a step, and observed on the way: both need the rows behind it."""
    counted = (
        rows.query()
        .filter(value().field("x") >= 5)
        .aggregate(
            {"n": count(), "largest": value().field("x").max()},
            group_by={"y": value().field("y")},
        )
        .collect()
    )
    observed = await counted.observe()
    assert observed.type.dims[0].name == "y"
    return context.run_step("total", total_of, {"counted": counted})


async def test_a_query_node_can_be_observed_and_read_by_a_step(database: Database) -> None:
    """Observing a query node answers with its rows, and a step handed it receives them."""
    job = await jobs(database).create(query_then_step, {"rows": QUERY_ROWS})
    settled = await run(database, job, FunctionRegistry([query_then_step, total_of]), settings=FAST)
    assert settled.state == "succeeded", await explain(database, job)
    # Both rows pass the filter and group apart, so each group counts one.
    assert await jobs(database).result(job) == Array.scalar(2, int)


def count_queries(monkeypatch: pytest.MonkeyPatch) -> list[QueryPlan]:
    """Record every query the evaluator runs from here on (it still runs them)."""
    ran: list[QueryPlan] = []
    query_arrays = local_query.query_arrays

    def counting(query: QueryPlan, inputs: Sequence[Array[Any]]) -> Array[Any]:
        ran.append(query)
        return query_arrays(query, inputs)

    monkeypatch.setattr(local_query, "query_arrays", counting)
    return ran


async def test_a_later_job_runs_the_same_queries_again(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job over the same arrays runs the queries again rather than reading another job's
    answer, which could differ for SQL that varies."""
    registry = FunctionRegistry([query_chain])
    first = await jobs(database).create(query_chain, {"rows": QUERY_ROWS})
    assert (await run(database, first, registry, settings=FAST)).state == "succeeded"
    ran = count_queries(monkeypatch)

    second = await jobs(database).create(query_chain, {"rows": QUERY_ROWS})
    assert (await run(database, second, registry, settings=FAST)).state == "succeeded"

    assert len(ran) == 2
    assert await jobs(database).result(second) == QUERY_CHAIN_RESULT


async def test_a_second_attempt_runs_its_queries_again(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A query's result is not kept between attempts: a second attempt of the invocation runs
    each query again and reaches the same answer."""
    global GATE
    GATE = anyio.Event()  # never opened: the first attempt is killed with its queries answered
    ran = count_queries(monkeypatch)
    registry = FunctionRegistry([gated_query_chain])
    job = await jobs(database).create(gated_query_chain, {"rows": QUERY_ROWS})
    runner = await a_runner(database, job, gated_query_chain)

    async def both_queries_ran() -> bool:
        return len(ran) == 2

    async with runner_task(runner) as outcome:
        await wait_until(in_the_body)
        await wait_until(both_queries_ran)
        outcome.kill()
    assert outcome.settled is None
    GATE = None
    ran.clear()

    settled = await run(database, job, registry, settings=FAST)
    assert settled.state == "succeeded", await explain(database, job)
    assert [a.state for a in await attempts_of(database, job)] == ["abandoned", "completed"]
    assert len(ran) == 2
    assert await jobs(database).result(job) == QUERY_CHAIN_RESULT


@pytest.mark.parametrize("error", UNREADABLE_REQUEST)
async def test_a_step_whose_request_cannot_be_read_fails_that_attempt_alone(
    database: Database, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """A recorded request that cannot be turned into a call is this attempt's own failure, and no
    retry fixes it: the row is closed failed rather than abandoned, and the job's other step runs.
    An error left out of that set would abandon the row instead, and reconciliation would hand the
    same unreadable request to attempt after attempt.
    """
    original = PostgresEntityStorage.load
    refused: list[Any] = []

    async def refuse_one_request(self: PostgresEntityStorage, ref: Any, **kwargs: Any) -> Any:
        loader = getattr(ref, "loader", None)
        is_request = loader is not None and loader.static_type_id == StepRequest.fxtr_type_id()
        if is_request and not refused:
            refused.append(ref)  # one of the two mapped steps; the other is left readable
            raise error(f"{ref.entity_id} cannot be read")
        return await original(self, ref, **kwargs)

    monkeypatch.setattr(PostgresEntityStorage, "load", refuse_one_request)
    job = await jobs(database).create(double_each, {"ids": IDS})
    settled = await run(database, job, FunctionRegistry([double_each, double]), settings=FAST)
    assert settled.state == "stopped"  # the failed step leaves the job with nothing left to do
    assert sorted(a.state for a in await step_attempts(database)) == ["completed", "failed"]
    assert STEP_CALLS[0] == 1  # the readable request ran; the unreadable one never reached a body


async def test_what_reading_a_request_that_is_not_one_raises_is_in_the_unreadable_set(
    database: Database,
) -> None:
    """The set above is a list of classes, so a parametrized test over it cannot notice one that
    is missing. This reads an entity that is not a step request at all, the way the runner does,
    and pins what that raises to the set — the case that would otherwise abandon the row and hand
    the same bytes to the next attempt, for as long as the job lives."""
    entities = PostgresEntityStorage(database)
    stored = await entities.store(Array.scalar(1, int))
    async with database.transaction() as tx:
        await entities.flush(tx)

    fresh = PostgresEntityStorage(database)  # not holding what the first one stored
    with pytest.raises(UNREADABLE_REQUEST):
        await fresh.load(stored.entity_id.bind_type(StepRequest))
