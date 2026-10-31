"""The execution state and its policy: the lease (heartbeat, takeover, eviction), job attempts and
what they cover, what is due and starting it (idempotently, under concurrent transactions), the
close hook, status, resume, cancellation, and the step cache's resolution rules."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from postgres_testing import (
    OWNER,
    a_client,
    a_code_version,
    a_job,
    a_request,
    active,
    age_heartbeat,
    assert_policy_agrees,
    expand_children,
    pending,
    produced_by,
    request,
    run_blocked,
    running_root,
    set_attempt_state,
    set_job_attempt_state,
    settle,
    start_invocation,
    start_root,
    start_step,
    take,
)
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from fxtr.entity_defns.array import Array, ArrayType
from fxtr.project.client import CacheConflict
from fxtr.runner import nodes
from fxtr.runner.session import AttemptError, AttemptID

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres import execution, ledger  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.entities import PostgresEntityStorage  # noqa: E402
from fxtr.runner.postgres.execution import (  # noqa: E402
    ConflictingEntry,
    Done,
    JobRunningElsewhereError,
    NotCancellableError,
    Pending,
    Ready,
    RunIt,
    StaleAttemptError,
    Started,
    StartedStep,
    StartInvocation,
    StartStep,
    Stopped,
)
from fxtr.runner.postgres.jobs import Jobs  # noqa: E402
from fxtr.runner.postgres.ledger import Emission, JobID, StepResultID  # noqa: E402
from fxtr.runner.postgres.schema import attempts as attempts_table  # noqa: E402

STALE_AFTER = timedelta(seconds=30)
"""The staleness threshold these tests judge leases by; the policy has no default of its own."""

pytestmark = pytest.mark.anyio

BOOM = AttemptError(type_name="RuntimeError", message="boom", traceback="")


async def close(
    database: Database,
    attempt_id: AttemptID,
    state: execution.ClosedState,
    *,
    error: AttemptError | None = None,
    reason: str | None = None,
) -> None:
    """Close a running workflow attempt the way its runner would: under the guard, then settle the
    job attempt in a transaction of its own."""
    async with database.transaction() as tx:
        act = await execution.guard(tx, attempt_id, ending=True)
        await execution.close_attempt(tx, act, state, error=error, reason=reason)
        job_id = act.job_id
    await settle(database, job_id)


async def finish(database: Database, attempt_id: AttemptID, value: int) -> Any:
    """Have a running workflow attempt emit one source array, return it, and materialize it as
    the invocation's result."""
    entities = PostgresEntityStorage(database)
    source = nodes.SourceArray(name="x", array=await entities.store(Array.scalar(value, int)))
    async with database.transaction() as tx:
        act = await execution.guard(tx, attempt_id, ending=True)
        await entities.flush(tx)
        await ledger.record_emissions(tx, act, [Emission(source, ArrayType.of(int))])
        await ledger.record_return(tx, act, source.content_id())
        await execution.materialize_result(tx, act, source.array)
        job_id = act.job_id
    await settle(database, job_id)
    return source.array


async def complete(database: Database, attempt_id: AttemptID, value: int) -> Any:
    """Complete a running step attempt with a scalar result, the way its runner would."""
    entities = PostgresEntityStorage(database)
    ref = await entities.store(Array.scalar(value, int))
    async with database.transaction() as tx:
        act = await execution.guard_step(tx, attempt_id, ending=True)
        await entities.flush(tx)
        await execution.complete_step(tx, act, ref)
    return ref


async def attempts_of(database: Database, job_id: JobID, pathname: str) -> list[Any]:
    async with database.transaction() as tx:
        invocation = await ledger.find_invocation(tx, job_id, pathname)
        return await execution.list_attempts(tx, invocation.id)


async def all_step_attempts(database: Database) -> list[Any]:
    async with database.transaction() as tx:
        rows = (
            await tx.execute(attempts_table.select().where(attempts_table.c.kind == "step"))
        ).all()
    return list(rows)


async def status_of(database: Database, job_id: JobID, pathname: str) -> Any:
    async with database.transaction() as tx:
        invocation = await ledger.find_invocation(tx, job_id, pathname)
        return await execution.status(tx, job_id, invocation)


async def step_status_of(database: Database, job_id: JobID, key: str) -> Any:
    """The step's status, after checking the job's status relations against the write paths."""
    await assert_policy_agrees(database, job_id)
    async with database.transaction() as tx:
        return await execution.step_status(tx, job_id, key)


async def state_of(database: Database, job_id: JobID, pathname: str) -> str:
    """The invocation's displayed state, after checking the job's status relations against the
    write paths."""
    await assert_policy_agrees(database, job_id)
    async with database.transaction() as tx:
        invocation = await ledger.find_invocation(tx, job_id, pathname)
        return (await execution.invocation_state(tx, invocation))[0]


async def resume(database: Database, job_id: JobID, owner: str = OWNER) -> Any:
    code_version = await a_code_version(database)
    async with database.transaction() as tx:
        return await execution.resume(
            tx, job_id, owner=owner, code_version=code_version, take_over_after=STALE_AFTER
        )


async def heartbeat(database: Database, job_attempt_id: Any, owner: str = OWNER) -> Any:
    async with database.transaction() as tx:
        return await execution.heartbeat(tx, job_attempt_id, owner)


# ---- the lease ----


async def test_a_job_attempt_holds_a_lease_its_owner_refreshes(database: Database) -> None:
    job_id = await a_job(database)
    job_attempt = await active(database, job_id)
    assert job_attempt.owner == OWNER and job_attempt.fresh(STALE_AFTER) and not job_attempt.evicted
    assert await heartbeat(database, job_attempt.id, owner="someone else") is None
    assert await heartbeat(database, job_attempt.id) == "active"
    await age_heartbeat(database, job_attempt.id, 60)
    assert not (await active(database, job_id)).fresh(STALE_AFTER)
    assert await heartbeat(database, job_attempt.id) == "active"  # refreshed: fresh again
    assert (await active(database, job_id)).fresh(STALE_AFTER)


async def test_the_guard_refuses_an_attempt_that_is_no_longer_running_and_only_that(
    database: Database,
) -> None:
    """Staleness informs, it does not act: an attempt whose owner's heartbeat lapsed still writes.
    Eviction is what revokes the right, by abandoning the row."""
    job_id, attempt_id = await running_root(database)
    job_attempt = await active(database, job_id)
    await age_heartbeat(database, job_attempt.id, 60)
    assert not (await active(database, job_id)).fresh(STALE_AFTER)
    async with database.transaction() as tx:
        act = await execution.guard(tx, attempt_id)
    assert act.id == attempt_id and act.owner_job_attempt_id == job_attempt.id
    async with database.transaction() as tx:
        await execution.evict(tx, job_attempt, "test")
    with pytest.raises(StaleAttemptError, match="abandoned"):
        async with database.transaction() as tx:
            await execution.guard(tx, attempt_id)


async def test_resume_on_a_fresh_lease_is_refused_whoever_asks(database: Database) -> None:
    """A live lease is a live lease: the owner string decides nothing, so the process holding the
    job attempt is refused like anyone else (it widens its scope through ``extend_scope``)."""
    job_id = await a_job(database)
    with pytest.raises(JobRunningElsewhereError):
        await resume(database, job_id, owner="someone else")
    with pytest.raises(JobRunningElsewhereError):
        await resume(database, job_id)


async def test_resume_on_a_stale_lease_evicts_and_starts_the_next_job_attempt(
    database: Database,
) -> None:
    job_id, attempt_id = await running_root(database)
    first = await active(database, job_id)
    await age_heartbeat(database, first.id, 60)
    taken = await resume(database, job_id, owner="someone else")
    assert taken.seq == 2 and taken.owner == "someone else" and taken.fresh(STALE_AFTER)
    async with database.transaction() as tx:
        old = await execution.get_job_attempt(tx, first.id)
        abandoned = await execution.get_attempt(tx, attempt_id)
    assert old.evicted and old.evicted_reason == "taken over by someone else"
    assert old.state == "stopped"  # nothing of it was live once evicted
    assert abandoned.state == "abandoned" and abandoned.reason == "taken over by someone else"
    assert await heartbeat(database, first.id) is None  # the old process has lost the job
    # The new job attempt is due to run the root again.
    assert abandoned.invocation_id is not None
    assert await pending(database, job_id) == [StartInvocation(abandoned.invocation_id)]


async def test_takeover_waits_for_an_open_heartbeat_then_refuses(database: Database) -> None:
    """The owner's heartbeat is in flight (its transaction holds the row) when a takeover arrives:
    the takeover blocks on the row, and once the heartbeat commits it finds the lease fresh and is
    refused."""
    job_id, attempt_id = await running_root(database)
    first = await active(database, job_id)
    await age_heartbeat(database, first.id, 60)
    open_heartbeat = database.transaction()
    tx1 = await open_heartbeat.__aenter__()
    assert await execution.heartbeat(tx1, first.id, OWNER) == "active"

    async def takeover() -> str:
        with pytest.raises(JobRunningElsewhereError):
            await resume(database, job_id, owner="someone else")
        return "refused"

    async def commit_heartbeat() -> None:
        await open_heartbeat.__aexit__(None, None, None)

    assert await run_blocked(takeover, commit_heartbeat) == "refused"
    async with database.transaction() as tx:
        kept = await execution.get_job_attempt(tx, first.id)
        assert (await execution.get_attempt(tx, attempt_id)).state == "running"
    assert kept.active and not kept.evicted and kept.fresh(STALE_AFTER)


async def test_a_resume_settles_a_job_attempt_whose_process_died_before_settling(
    database: Database,
) -> None:
    """An attempt's close does not settle; the owning runner does, on its next loop pass. A
    process that dies in between leaves the job attempt active with the work finished or exhausted.
    The next resume, by anyone, settles it by the same rule before doing anything else."""
    entities = PostgresEntityStorage(database)
    # Finished: the root has its result, but the job attempt was never settled.
    job_id = await a_job(database)
    started = await start_root(database, job_id)
    source = nodes.SourceArray(name="x", array=await entities.store(Array.scalar(1, int)))
    async with database.transaction() as tx:
        act = await execution.guard(tx, started.attempt_id, ending=True)
        await entities.flush(tx)
        await ledger.record_emissions(tx, act, [Emission(source, ArrayType.of(int))])
        await ledger.record_return(tx, act, source.content_id())
        await execution.materialize_result(tx, act, source.array)
    first = await active(database, job_id)
    assert first.state == "active"
    await age_heartbeat(database, first.id, 60)
    finished = await resume(database, job_id, owner="someone else")
    assert isinstance(finished, execution.AlreadyFinished)
    assert finished.invocation.result is not None and finished.job_attempt is not None
    assert finished.job_attempt.state == "succeeded" and not finished.job_attempt.evicted
    async with database.transaction() as tx:
        (settled,) = await execution.list_job_attempts(tx, job_id)
    assert settled.id == finished.job_attempt.id and settled.state == "succeeded"
    # Exhausted: the root failed, nothing is due, and the job attempt was never settled.
    job_id = await a_job(database)
    started = await start_root(database, job_id)
    async with database.transaction() as tx:
        act = await execution.guard(tx, started.attempt_id, ending=True)
        await execution.close_attempt(tx, act, "failed", error=BOOM)
    assert (await active(database, job_id)).state == "active"
    await age_heartbeat(database, (await active(database, job_id)).id, 60)
    taken = await resume(database, job_id, owner="someone else")
    async with database.transaction() as tx:
        first, second = await execution.list_job_attempts(tx, job_id)
    # Settled stopped, by the rule, rather than evicted; the resume then started the next one.
    assert first.state == "stopped" and not first.evicted
    assert isinstance(taken, execution.JobAttemptRecord)
    assert second.id == taken.id and second.active and second.seq == 2


async def test_starting_under_a_job_attempt_being_evicted_is_refused(database: Database) -> None:
    """A start takes the job attempt's row first: one that arrives while an eviction is in flight
    waits for it, then finds the job attempt evicted and starts nothing, so the eviction's sweep is
    the whole of what the job attempt ever owned."""
    job_id = await a_job(database)
    job_attempt = await active(database, job_id)
    async with database.transaction() as tx:
        root = await ledger.find_invocation(tx, job_id, "root")
    evicting = database.transaction()
    tx1 = await evicting.__aenter__()
    assert await execution.evict(tx1, job_attempt, "test") == []  # uncommitted

    async def start() -> Any:
        async with database.transaction() as tx2:
            return await execution.start_attempt(tx2, job_attempt, StartInvocation(root.id))

    async def commit_eviction() -> None:
        await evicting.__aexit__(None, None, None)

    assert await run_blocked(start, commit_eviction) is None
    assert await attempts_of(database, job_id, "root") == []


async def test_a_step_start_reads_first_then_is_refused_by_an_eviction(database: Database) -> None:
    """A step's start makes its reads before taking the job attempt's row: one whose reads see
    a step to run, and that then waits on an eviction in flight, finds the job attempt evicted
    once it has the row and starts nothing."""
    job_id, attempt_id = await running_root(database)
    assert isinstance(
        await request(database, job_id, attempt_id, "k", await a_request(database, 1)), RunIt
    )
    job_attempt = await active(database, job_id)
    evicting = database.transaction()
    tx1 = await evicting.__aenter__()
    assert await execution.evict(tx1, job_attempt, "test") == [attempt_id]  # uncommitted

    async def start() -> Any:
        async with database.transaction() as tx2:
            return await execution.start_attempt(tx2, job_attempt, StartStep("k"))

    async def commit_eviction() -> None:
        await evicting.__aexit__(None, None, None)

    assert await run_blocked(start, commit_eviction) is None
    assert await all_step_attempts(database) == []
    async with database.transaction() as tx:
        assert await execution.get_cache_entry(tx, "k") is None


async def test_settling_waits_for_an_in_flight_start(database: Database) -> None:
    """Settling takes the job attempt's row first too: it waits for a start in flight and then
    sees the attempt it inserted as running, rather than settling the job attempt stopped with
    work about to run under it."""
    job_id = await a_job(database)
    job_attempt = await active(database, job_id)
    async with database.transaction() as tx:
        root = await ledger.find_invocation(tx, job_id, "root")
    starting = database.transaction()
    tx1 = await starting.__aenter__()
    started = await execution.start_attempt(tx1, job_attempt, StartInvocation(root.id))
    assert isinstance(started, Started)  # uncommitted

    async def settle_now() -> Any:
        return await settle(database, job_id)

    async def commit_start() -> None:
        await starting.__aexit__(None, None, None)

    assert await run_blocked(settle_now, commit_start) is None
    assert (await active(database, job_id)).state == "active"
    (running,) = await attempts_of(database, job_id, "root")
    assert running.state == "running"


async def test_settling_waits_for_an_in_flight_ending(database: Database) -> None:
    """The transaction that ends an attempt takes the job attempt's row first, so a settle
    running alongside the root's completion waits for it and then sees the root finished, rather
    than reading the root as unfinished and then the attempt as not running and settling a job
    that succeeded ``stopped``."""
    entities = PostgresEntityStorage(database)
    job_id = await a_job(database)
    started = await start_root(database, job_id)
    source = nodes.SourceArray(name="x", array=await entities.store(Array.scalar(1, int)))
    ending = database.transaction()
    tx1 = await ending.__aenter__()
    act = await execution.guard(tx1, started.attempt_id, ending=True)
    await entities.flush(tx1)
    await ledger.record_emissions(tx1, act, [Emission(source, ArrayType.of(int))])
    await ledger.record_return(tx1, act, source.content_id())
    await execution.materialize_result(tx1, act, source.array)  # uncommitted

    async def settle_now() -> Any:
        return await settle(database, job_id)

    async def commit_ending() -> None:
        await ending.__aexit__(None, None, None)

    settled = await run_blocked(settle_now, commit_ending)
    assert settled is not None and settled.state == "succeeded"


async def test_an_ending_waits_for_a_settle_holding_the_job_attempt(database: Database) -> None:
    """The other order: a completion arriving while a settle holds the job attempt's row waits,
    and the settle, seeing the attempt running, does not settle; the completion then lands and
    the next settle succeeds the job attempt."""
    job_id = await a_job(database)
    started = await start_root(database, job_id)
    holding = database.transaction()
    tx1 = await holding.__aenter__()
    assert (await execution.settle(tx1, job_id)).settled is None  # holds the row, uncommitted

    async def complete_root() -> Any:
        return await finish(database, started.attempt_id, 1)

    async def release() -> None:
        await holding.__aexit__(None, None, None)

    await run_blocked(complete_root, release)
    async with database.transaction() as tx:
        (settled,) = await execution.list_job_attempts(tx, job_id)
    assert settled.state == "succeeded"
    (done,) = await attempts_of(database, job_id, "root")
    assert done.state == "completed"


async def test_ending_needs_the_ending_guard(database: Database) -> None:
    """Ending an attempt under a guard taken without ``ending=True`` is a programming error."""
    job_id = await a_job(database)
    started = await start_root(database, job_id)
    with pytest.raises(AssertionError, match="ending=True"):
        async with database.transaction() as tx:
            act = await execution.guard(tx, started.attempt_id)
            await execution.close_attempt(tx, act, "failed", error=BOOM)


async def test_two_resumes_of_an_untaken_job_take_it_once(database: Database) -> None:
    """Two processes resuming a job nobody holds serialize on the job's row: the second waits for
    the first's job attempt to exist and is then refused, cleanly."""
    job_id = JobID(uuid.uuid4())
    async with database.transaction() as tx:
        await ledger.create_job(tx, job_id, "wf", "root", inputs={})
    first_tx = database.transaction()
    tx1 = await first_tx.__aenter__()
    code_version = await a_code_version(database)
    first = await execution.resume(tx1, job_id, owner="one", code_version=code_version)
    assert isinstance(first, execution.JobAttemptRecord) and first.seq == 1  # uncommitted

    async def second() -> str:
        with pytest.raises(JobRunningElsewhereError):
            await resume(database, job_id, owner="two")
        return "refused"

    async def commit_first() -> None:
        await first_tx.__aexit__(None, None, None)

    assert await run_blocked(second, commit_first) == "refused"
    async with database.transaction() as tx:
        (only,) = await execution.list_job_attempts(tx, job_id)
    assert only.owner == "one"


async def test_evict_sweeps_what_the_job_attempt_owns_and_is_idempotent(
    database: Database,
) -> None:
    job_id, attempt_id = await running_root(database)
    req = await a_request(database, 1)
    await request(database, job_id, attempt_id, "k", req)
    started_step = await start_step(database, job_id, "k")
    assert started_step is not None
    job_attempt = await active(database, job_id)
    async with database.transaction() as tx:
        swept = await execution.evict(tx, job_attempt, "test")
    assert swept is not None and set(swept) == {attempt_id, started_step.attempt.id}
    async with database.transaction() as tx:
        assert (await execution.get_attempt(tx, attempt_id)).state == "abandoned"
        assert (await execution.get_attempt(tx, started_step.attempt.id)).state == "abandoned"
        assert await execution.evict(tx, job_attempt, "again") is None
        evicted = await execution.get_job_attempt(tx, job_attempt.id)
        # Nothing starts under an evicted job attempt, due or not.
        assert (await execution.pending_work(tx, evicted)) == []
        assert (
            await execution.start_attempt(
                tx,
                job_attempt,
                StartInvocation((await ledger.find_invocation(tx, job_id, "root")).id),
            )
            is None
        )
    assert evicted.evicted_reason == "test" and evicted.state == "stopped"


# ---- job attempts, what is due, and starting it ----


async def test_a_job_starts_with_an_active_job_attempt_due_its_root(database: Database) -> None:
    job_id = await a_job(database)
    job_attempt = await active(database, job_id)
    assert job_attempt.seq == 1
    async with database.transaction() as tx:
        root = await ledger.find_invocation(tx, job_id, "root")
    assert await pending(database, job_id) == [StartInvocation(root.id)]
    # A second active job attempt for the job is refused by the database.
    code_version = await a_code_version(database)
    with pytest.raises(IntegrityError):
        async with database.transaction() as tx:
            await execution.create_job_attempt(tx, job_id, owner="other", code_version=code_version)
    started = await start_root(database, job_id)
    (running,) = await attempts_of(database, job_id, "root")
    assert running.id == started.attempt_id and running.state == "running"
    assert running.owner_job_attempt_id == job_attempt.id
    # Expansion creates children, which the active job attempt is due to run, in creation order;
    # the running root is not listed, and neither is anything of another job.
    _, children = await expand_children(database, started)
    await a_job(database)
    assert await pending(database, job_id) == [StartInvocation(c.id) for c in children]


@pytest.mark.parametrize("size", [1, 32])
async def test_pending_work_reads_due_items_in_two_queries(database: Database, size: int) -> None:
    """What is due costs two queries however many steps the job requested: settle derives it while
    holding the job attempt's row."""
    job_id, root = await running_root(database)
    job_attempt = await active(database, job_id)
    req = await a_request(database, 1)
    keys = [f"step/{i:03}" for i in range(size)]
    async with database.transaction() as tx:
        act = await execution.guard(tx, root)
        for key in reversed(keys):
            await ledger.record_step_request(tx, act, key, req, "s")
    statements = 0

    def count(*args: Any) -> None:
        nonlocal statements
        statements += 1

    async with database.transaction() as tx:
        event.listen(database.engine.sync_engine, "before_cursor_execute", count)
        try:
            due = await execution.pending_work(tx, job_attempt)
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", count)
    assert due == [StartStep(key) for key in keys]
    assert statements == 2


async def test_starting_is_idempotent_and_refuses_what_cannot_start(database: Database) -> None:
    job_id = await a_job(database)
    job_attempt = await active(database, job_id)
    async with database.transaction() as tx:
        root = await ledger.find_invocation(tx, job_id, "root")
    started = await start_root(database, job_id)
    async with database.transaction() as tx:
        # Already running: nothing starts.
        assert await execution.start_attempt(tx, job_attempt, StartInvocation(root.id)) is None
    assert len(await attempts_of(database, job_id, "root")) == 1
    await finish(database, started.attempt_id, 1)
    async with database.transaction() as tx:
        # Finished, and the job attempt settled: nothing starts.
        assert await execution.start_attempt(tx, job_attempt, StartInvocation(root.id)) is None
        (job_attempt,) = await execution.list_job_attempts(tx, job_id)
    assert job_attempt.state == "succeeded"


async def test_two_processes_starting_one_invocation_start_it_once(database: Database) -> None:
    """Two transactions inserting the invocation's attempt collide on the one-running index: the
    second waits for the first to commit and then finds nothing to start."""
    job_id = await a_job(database)
    job_attempt = await active(database, job_id)
    async with database.transaction() as tx:
        root = await ledger.find_invocation(tx, job_id, "root")
    first_tx = database.transaction()
    tx1 = await first_tx.__aenter__()
    first = await execution.start_attempt(tx1, job_attempt, StartInvocation(root.id))
    assert isinstance(first, Started)  # uncommitted

    async def second() -> Any:
        async with database.transaction() as tx2:
            return await execution.start_attempt(tx2, job_attempt, StartInvocation(root.id))

    async def commit_first() -> None:
        await first_tx.__aexit__(None, None, None)

    assert await run_blocked(second, commit_first) is None
    assert [a.id for a in await attempts_of(database, job_id, "root")] == [first.attempt_id]


async def test_a_job_attempt_tries_each_invocation_once_plus_replacements_for_abandonment(
    database: Database,
) -> None:
    job_id = await a_job(database)
    first = await start_root(database, job_id)
    await close(database, first.attempt_id, "abandoned", reason="runner stopped")
    # Abandoned: due again under the same job attempt, which stays active.
    assert len(await pending(database, job_id)) == 1
    assert (await active(database, job_id)).state == "active"
    second = await start_root(database, job_id)
    await close(database, second.attempt_id, "failed", error=BOOM)
    # Failed: no retry (the job attempt already tried this invocation), nothing is running and
    # nothing is due, so the hook settled the job attempt as stopped.
    assert len(await attempts_of(database, job_id, "root")) == 2
    async with database.transaction() as tx:
        assert await execution.active_job_attempt(tx, job_id) is None
        (job_attempt,) = await execution.list_job_attempts(tx, job_id)
    assert job_attempt.state == "stopped" and job_attempt.settled_at is not None
    # Resume: a new job attempt, one new attempt; an interruption under it is not retried either.
    resumed = await resume(database, job_id)
    assert resumed.seq == 2 and resumed.state == "active"
    assert len(await pending(database, job_id)) == 1
    third = await start_root(database, job_id)
    assert (await attempts_of(database, job_id, "root"))[-1].owner_job_attempt_id == resumed.id
    await close(database, third.attempt_id, "interrupted", reason="a child failed")
    assert len(await attempts_of(database, job_id, "root")) == 3
    async with database.transaction() as tx:
        assert (await execution.get_job_attempt(tx, resumed.id)).state == "stopped"


async def test_a_resume_leaves_live_attempts_of_a_draining_job_attempt_alone(
    database: Database,
) -> None:
    """Work still running under a settled (soft-cancelled) job attempt keeps running and writing
    under its fresh lease; a resume starts the next job attempt, which is due to run whatever is
    unfinished and not running, as its old attempts end."""
    job_id = await a_job(database)
    root = await start_root(database, job_id)
    _, children = await expand_children(database, root)
    started = [await start_invocation(database, job_id, child) for child in children]
    await set_job_attempt_state(database, job_id, "cancelled")  # a soft cancel, staged
    assert isinstance(await status_of(database, job_id, "root/child[k:0]"), Stopped)
    resumed = await resume(database, job_id)
    assert resumed.seq == 2 and resumed.state == "active"
    # Every attempt of the job is still live, so nothing is due under the new job attempt yet.
    assert await pending(database, job_id) == []
    for child_attempt in started:
        await close(database, child_attempt.attempt_id, "failed", error=BOOM)  # still running
    # Both children are due under the new job attempt, which has not tried them; the root is not
    # while its old attempt runs.
    assert set(await pending(database, job_id)) == {StartInvocation(c.id) for c in children}
    assert [a.state for a in await attempts_of(database, job_id, "root/child[k:1]")] == ["failed"]
    await close(database, root.attempt_id, "interrupted", reason="children failed")
    assert set(await pending(database, job_id)) == {
        StartInvocation(root.invocation.id),
        *(StartInvocation(c.id) for c in children),
    }


async def test_resume_refuses_a_finished_job_and_an_unknown_one(database: Database) -> None:
    job_id = await a_job(database)
    code_version = await a_code_version(database)
    started = await start_root(database, job_id)
    await finish(database, started.attempt_id, 1)
    async with database.transaction() as tx:
        (job_attempt,) = await execution.list_job_attempts(tx, job_id)
        assert job_attempt.state == "succeeded"
        assert (await execution.get_attempt(tx, started.attempt_id)).state == "completed"
        finished = await execution.resume(tx, job_id, owner=OWNER, code_version=code_version)
        assert isinstance(finished, execution.AlreadyFinished)
        assert finished.invocation.id == started.invocation.id
        with pytest.raises(ledger.NotFoundError):
            await execution.resume(tx, JobID(uuid.uuid4()), owner=OWNER, code_version=code_version)


# ---- status ----


async def test_status_of_a_child_is_done_pending_or_stopped(database: Database) -> None:
    job_id = await a_job(database)
    root = await start_root(database, job_id)
    _, (first, second) = await expand_children(database, root)
    assert isinstance(await status_of(database, job_id, first.pathname), Pending)  # due
    first_started = await start_invocation(database, job_id, first)
    assert isinstance(await status_of(database, job_id, first.pathname), Pending)  # running
    await close(database, first_started.attempt_id, "failed", error=BOOM)
    stopped = await status_of(database, job_id, first.pathname)
    assert isinstance(stopped, Stopped) and stopped.error == BOOM
    assert stopped.reason == f"child invocation {first.pathname!r} failed"
    # Finish the second child: done, whatever the job attempts are doing.
    second_started = await start_invocation(database, job_id, second)
    ref = await finish(database, second_started.attempt_id, 2)
    done = await status_of(database, job_id, second.pathname)
    assert isinstance(done, Done) and done.result.entity_id == ref.entity_id
    # With no active job attempt nothing unfinished is pending, running or not.
    await set_job_attempt_state(database, job_id, "cancelled")
    assert isinstance(await status_of(database, job_id, "root"), Stopped)  # root is running
    assert isinstance(await status_of(database, job_id, second.pathname), Done)


async def test_invocation_state_follows_the_attempts(database: Database) -> None:
    job_id = await a_job(database)
    assert await state_of(database, job_id, "root") == "queued"  # due
    started = await start_root(database, job_id)
    assert await state_of(database, job_id, "root") == "running"
    await close(database, started.attempt_id, "interrupted", reason="a child failed")
    assert await state_of(database, job_id, "root") == "interrupted"  # and the job attempt stopped
    await resume(database, job_id)
    assert await state_of(database, job_id, "root") == "queued"
    again = await start_root(database, job_id)
    await close(database, again.attempt_id, "abandoned", reason="gone")
    assert await state_of(database, job_id, "root") == "queued"  # a replacement is due
    await set_job_attempt_state(database, job_id, "cancelled")
    # Cancelled: the job attempt exists but nothing of it is due, so nothing will run the root.
    assert await state_of(database, job_id, "root") == "stopped"


# ---- steps: requests, the cache, and starting them ----


async def test_a_request_resolves_to_running_it_and_starting_takes_the_entry(
    database: Database,
) -> None:
    job_id, attempt_id = await running_root(database)
    req = await a_request(database, 1)
    resolution = await request(database, job_id, attempt_id, "k", req)
    assert resolution == RunIt(req.entity_id, replaces=None)
    assert await pending(database, job_id) == [StartStep("k")]
    assert isinstance(await step_status_of(database, job_id, "k"), Pending)  # due
    started = await start_step(database, job_id, "k")
    assert started is not None and started.step_name == "s"
    attempt = started.attempt
    assert attempt.kind == "step" and attempt.state == "running" and attempt.cache_key == "k"
    assert attempt.request == req.entity_id
    assert attempt.owner_job_attempt_id == (await active(database, job_id)).id
    async with database.transaction() as tx:
        entry = await execution.get_cache_entry(tx, "k")
        recorded = await ledger.get_step_request(tx, req.entity_id)
        made = await ledger.get_job_step_request(tx, job_id, "k")
    assert entry is not None and entry.request == req.entity_id and entry.attempt_id == attempt.id
    assert recorded.step_name == "s" and made is not None and made.request == req.entity_id
    assert await step_status_of(database, job_id, "k") == Pending(attempt.id)
    assert await pending(database, job_id) == []  # running: not due
    # Asking again in the same job is a match, now pending on the running attempt.
    assert await request(database, job_id, attempt_id, "k", req) == Pending(attempt.id)
    async with database.transaction() as tx:
        assert [e.cache_key for e in await execution.list_cache_entries(tx)] == ["k"]


async def test_two_jobs_starting_one_key_make_one_attempt(database: Database) -> None:
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database)
    req = await a_request(database, 1)
    assert isinstance(await request(database, job_a, attempt_a, "k", req), RunIt)
    assert isinstance(await request(database, job_b, attempt_b, "k", req), RunIt)
    started = await start_step(database, job_a, "k")
    assert started is not None
    # Job B's start finds the entry taken; its request is pending on job A's attempt.
    assert await start_step(database, job_b, "k") is None
    assert await step_status_of(database, job_b, "k") == Pending(started.attempt.id)
    assert await pending(database, job_b) == []
    assert len(await all_step_attempts(database)) == 1


async def test_a_step_start_whose_read_is_overtaken_while_it_waits_starts_nothing(
    database: Database,
) -> None:
    """A step's start resolves its request before taking the job attempt's row. If another job
    takes the key while the start waits for the row, the resolution it read is stale; taking
    the entry is conditional on it, so the start gives its attempt back and starts nothing."""
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database)
    req = await a_request(database, 1)
    assert isinstance(await request(database, job_a, attempt_a, "k", req), RunIt)
    assert isinstance(await request(database, job_b, attempt_b, "k", req), RunIt)
    job_attempt_a = await active(database, job_a)
    holding = database.transaction()
    tx1 = await holding.__aenter__()
    assert await execution.lock_job_attempt(tx1, job_attempt_a.id) is not None  # held

    async def start_a() -> Any:
        async with database.transaction() as tx2:
            return await execution.start_attempt(tx2, job_attempt_a, StartStep("k"))

    taken: list[StartedStep | None] = []

    async def b_takes_the_key_then_release() -> None:
        taken.append(await start_step(database, job_b, "k"))
        await holding.__aexit__(None, None, None)

    assert await run_blocked(start_a, b_takes_the_key_then_release) is None
    (started_b,) = taken
    assert started_b is not None
    (only,) = await all_step_attempts(database)
    assert str(only.id) == started_b.attempt.id
    assert await step_status_of(database, job_a, "k") == Pending(started_b.attempt.id)


async def test_two_takeovers_of_one_key_take_it_once(database: Database) -> None:
    """Two transactions replacing the same failed attempt as the entry's current collide on the
    conditional update: the second finds the entry already moved and starts nothing."""
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database)
    job_c, attempt_c = await running_root(database)
    req = await a_request(database, 1)
    await request(database, job_a, attempt_a, "k", req)
    failed = await start_step(database, job_a, "k")
    assert failed is not None
    await set_attempt_state(database, failed.attempt.id, "failed")
    assert await request(database, job_b, attempt_b, "k", req) == RunIt(
        req.entity_id, replaces=failed.attempt.id
    )
    assert await request(database, job_c, attempt_c, "k", req) == RunIt(
        req.entity_id, replaces=failed.attempt.id
    )
    b = await active(database, job_b)
    c = await active(database, job_c)
    first_tx = database.transaction()
    tx1 = await first_tx.__aenter__()
    taken = await execution.start_attempt(tx1, b, StartStep("k"))
    assert isinstance(taken, StartedStep)  # uncommitted

    async def second() -> Any:
        async with database.transaction() as tx2:
            return await execution.start_attempt(tx2, c, StartStep("k"))

    async def commit_first() -> None:
        await first_tx.__aexit__(None, None, None)

    assert await run_blocked(second, commit_first) is None
    async with database.transaction() as tx:
        entry = await execution.get_cache_entry(tx, "k")
    assert entry is not None and entry.attempt_id == taken.attempt.id
    assert await step_status_of(database, job_c, "k") == Pending(taken.attempt.id)


async def test_a_jobs_own_key_reuse_is_an_error_and_anothers_live_request_is_a_cache_conflict(
    database: Database,
) -> None:
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database)
    one, two = await a_request(database, 1), await a_request(database, 2)
    await request(database, job_a, attempt_a, "k", one)
    with pytest.raises(ledger.CacheAddressReuseError):
        await request(database, job_a, attempt_a, "k", two)  # the same job, a different request
    started = await start_step(database, job_a, "k")
    assert started is not None
    # Another job, a different request, while job A's attempt runs under a fresh lease: the key
    # is in conflict. The request is a fact all the same; nothing can run it there.
    conflict = await request(database, job_b, attempt_b, "k", two)
    assert (
        isinstance(conflict, ConflictingEntry) and conflict.entry.attempt_id == started.attempt.id
    )
    stopped = await step_status_of(database, job_b, "k")
    assert isinstance(stopped, Stopped) and "cache conflict at" in stopped.reason
    assert await pending(database, job_b) == []
    # Job B's report names what owns the key: job A's request, still running.
    report = await Jobs(database, PostgresEntityStorage(database)).status(job_b)
    assert report.cache_conflicts == [CacheConflict("k", "s", "running")]
    assert report.steps["k"].state == "conflict"
    # Clearing job B's conflicts leaves a running one: another job computes it now.
    async with database.transaction() as tx:
        (record,) = await execution.cache_conflicts(tx, job_b)
        assert record.cache_key == "k" and record.offered_step_result_id is None
        # A clear of an entry that offers a given step result leaves any other entry there.
        assert not await execution.clear_cache_entry(tx, "k", offering=StepResultID(uuid.uuid4()))
    assert await a_client(database).clear_cache_entries(conflicts_in=job_b) == 0
    async with database.transaction() as tx:
        assert await execution.get_cache_entry(tx, "k") is not None


async def test_an_overwriting_job_attempt_takes_a_completed_conflict_once(
    database: Database,
) -> None:
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database, overwrite_cache_conflicts=True)
    job_c, attempt_c = await running_root(database, overwrite_cache_conflicts=True)
    one, two, three = [await a_request(database, n) for n in (1, 2, 3)]
    await request(database, job_a, attempt_a, "k", one)
    started = await start_step(database, job_a, "k")
    assert started is not None
    await complete(database, started.attempt.id, 10)
    async with database.transaction() as tx:
        entry = await execution.get_cache_entry(tx, "k")
    assert entry is not None and entry.step_result_id is not None
    offered = entry.step_result_id
    # Jobs B and C overwrite: a different request's completed result is theirs to run over.
    assert await request(database, job_b, attempt_b, "k", two) == RunIt(
        two.entity_id, replaces=None, overwrites=offered
    )
    assert await request(database, job_c, attempt_c, "k", three) == RunIt(
        three.entity_id, replaces=None, overwrites=offered
    )
    assert await pending(database, job_b) == [StartStep("k")]
    # The first to start takes the entry; the other then finds B's attempt running there, which
    # is a conflict that is not overwritten.
    taken = await start_step(database, job_b, "k")
    assert taken is not None and taken.overwrote == offered
    assert await start_step(database, job_c, "k") is None
    stopped = await step_status_of(database, job_c, "k")
    assert isinstance(stopped, Stopped) and stopped.cache_conflict
    async with database.transaction() as tx:
        entry = await execution.get_cache_entry(tx, "k")
    assert entry is not None and entry.attempt_id == taken.attempt.id
    assert entry.step_result_id is None and entry.request == two.entity_id
    # Job A had not taken its result yet, so it now finds B's attempt in the way, as it would
    # after a clear of the key.
    stopped = await step_status_of(database, job_a, "k")
    assert isinstance(stopped, Stopped) and stopped.cache_conflict


async def test_adopting_a_step_result_depends_on_what_the_entry_holds(
    database: Database,
) -> None:
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database)
    one, two = await a_request(database, 1), await a_request(database, 2)

    async def adopt(result: Any, *, overwrite: bool = False) -> str:
        async with database.transaction() as tx:
            return await execution.adopt_step_result(
                tx, "k", result, overwrite_cache_conflicts=overwrite
            )

    async def offered() -> Any:
        async with database.transaction() as tx:
            entry = await execution.get_cache_entry(tx, "k")
        return None if entry is None else (entry.request, entry.step_result_id)

    async def clear() -> None:
        async with database.transaction() as tx:
            await execution.clear_cache_entry(tx, "k")

    async def completed_by(job_id: JobID, value: int) -> Any:
        started = await start_step(database, job_id, "k")
        assert started is not None
        await complete(database, started.attempt.id, value)
        async with database.transaction() as tx:
            entry = await execution.get_cache_entry(tx, "k")
            assert entry is not None and entry.step_result_id is not None
            return await ledger.get_step_result(tx, entry.step_result_id)

    await request(database, job_a, attempt_a, "k", one)
    first = await completed_by(job_a, 10)
    # No entry: adopted. The same result there: already.
    await clear()
    assert await adopt(first) == "adopted"
    assert await offered() == (one.entity_id, first.id)
    assert await adopt(first) == "already"
    # A different result of the same request: left alone.
    await clear()
    second = await completed_by(job_a, 20)
    assert await adopt(first) == "equal_request"
    # An attempt running there: left alone; once it has failed, adopted over it.
    await clear()
    await request(database, job_b, attempt_b, "k", two)
    running = await start_step(database, job_b, "k")
    assert running is not None
    assert await adopt(first) == "running"
    await set_attempt_state(database, running.attempt.id, "failed")
    assert await adopt(first) == "adopted"
    assert await offered() == (one.entity_id, first.id)
    # A different request's result: a conflict, adopted over only when asked.
    await clear()
    job_c, attempt_c = await running_root(database)
    await request(database, job_c, attempt_c, "k", two)
    other = await completed_by(job_c, 30)
    assert await adopt(first) == "conflict"
    assert await offered() == (two.entity_id, other.id)
    assert await adopt(first, overwrite=True) == "adopted"
    assert await offered() == (one.entity_id, first.id)
    assert second.id != first.id


async def test_a_dead_attempt_is_retried_or_its_key_taken_over(database: Database) -> None:
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database)
    job_c, attempt_c = await running_root(database)
    one, two = await a_request(database, 1), await a_request(database, 2)
    await request(database, job_a, attempt_a, "k", one)
    first = await start_step(database, job_a, "k")
    assert first is not None
    async with database.transaction() as tx:
        act = await execution.guard_step(tx, first.attempt.id, ending=True)
        await execution.close_step_attempt(tx, act, "failed", error=BOOM)
    # Job A does not retry its own failure; job B, an equal request, runs the step again and
    # becomes the entry's current.
    stopped = await step_status_of(database, job_a, "k")
    assert isinstance(stopped, Stopped) and "failed" in stopped.reason and stopped.error == BOOM
    assert await pending(database, job_a) == []
    # Nor does a StartStep for the key that was queued before the failure, and handled after it.
    assert await start_step(database, job_a, "k") is None
    assert await request(database, job_b, attempt_b, "k", one) == RunIt(
        one.entity_id, replaces=first.attempt.id
    )
    retried = await start_step(database, job_b, "k")
    assert retried is not None
    async with database.transaction() as tx:
        entry = await execution.get_cache_entry(tx, "k")
    assert entry is not None and entry.attempt_id == retried.attempt.id
    # Job A's own failure stays its answer for the key, whatever B does there: a resume of A
    # (a new job attempt, with no failure of its own) would take B's result instead.
    assert isinstance(await step_status_of(database, job_a, "k"), Stopped)
    # Job C, a different request, while the retry runs: a cache conflict.
    assert isinstance(await request(database, job_c, attempt_c, "k", two), ConflictingEntry)
    # Once the retry has failed too, nothing live owns the key: job C takes it over.
    await set_attempt_state(database, retried.attempt.id, "failed")
    assert await request(database, job_c, attempt_c, "k", two) == RunIt(
        two.entity_id, replaces=retried.attempt.id
    )
    taken = await start_step(database, job_c, "k")
    assert taken is not None
    async with database.transaction() as tx:
        entry = await execution.get_cache_entry(tx, "k")
    assert entry is not None and entry.request == two.entity_id
    assert entry.attempt_id == taken.attempt.id


async def test_a_stale_owners_step_is_waited_on_until_it_is_evicted(database: Database) -> None:
    """A running attempt is live whatever its owner's heartbeat says: job B keeps waiting on job
    A's step while A's lease is stale. Only a user action on job A (here a takeover by a resume)
    abandons the attempt, and then B runs the step itself."""
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database, owner="other")
    req = await a_request(database, 1)
    await request(database, job_a, attempt_a, "k", req)
    started = await start_step(database, job_a, "k")
    assert started is not None
    assert await request(database, job_b, attempt_b, "k", req) == Pending(started.attempt.id)
    await age_heartbeat(database, (await active(database, job_a)).id, 60)
    assert await request(database, job_b, attempt_b, "k", req) == Pending(started.attempt.id)
    assert await pending(database, job_b) == []
    async with database.transaction() as tx:
        await execution.guard_step(tx, started.attempt.id)  # A's attempt still writes
    await resume(database, job_a, owner="someone else")  # takes job A over
    assert await request(database, job_b, attempt_b, "k", req) == RunIt(
        req.entity_id, replaces=started.attempt.id
    )
    taken = await start_step(database, job_b, "k")
    assert taken is not None and taken.attempt.id != started.attempt.id
    with pytest.raises(StaleAttemptError):
        async with database.transaction() as tx:
            await execution.guard_step(tx, started.attempt.id)


async def test_a_completed_result_is_taken_by_each_job_and_the_binding_survives_a_clear(
    database: Database,
) -> None:
    """Completion binds nobody: each job takes the result itself, writing its own binding, and is
    done only then. The binding is a ledger fact: clearing the key afterwards changes nothing for
    the jobs that took it."""
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database)
    one = await a_request(database, 1)
    await request(database, job_a, attempt_a, "k", one)
    first = await start_step(database, job_a, "k")
    assert first is not None
    ref = await complete(database, first.attempt.id, 7)
    # Job A's request is not bound yet: the result is on offer, and its own attempt takes it.
    assert await step_status_of(database, job_a, "k") == Ready(produced_by(first.attempt.id))
    async with database.transaction() as tx:
        assert await ledger.get_step_binding(tx, job_a, "k") is None
    taken_a = await take(database, attempt_a, "k")
    assert taken_a is not None and taken_a.entity_id == ref.entity_id
    done = await step_status_of(database, job_a, "k")
    assert isinstance(done, Done) and done.result.entity_id == ref.entity_id
    # Job B, requesting the same call afterwards, finds the result on offer and takes it too.
    offered = await request(database, job_b, attempt_b, "k", one)
    assert isinstance(offered, execution.Completed) and offered.step_result_id == produced_by(
        first.attempt.id
    )
    assert offered.result.entity_id == ref.entity_id
    assert await step_status_of(database, job_b, "k") == Ready(produced_by(first.attempt.id))
    assert (await take(database, attempt_b, "k")) is not None
    assert await take(database, attempt_b, "k") is not None  # idempotent: already bound
    assert isinstance(await step_status_of(database, job_b, "k"), Done)
    async with database.transaction() as tx:
        binding = await ledger.get_step_binding(tx, job_b, "k")
    assert binding is not None and binding.step_result_id == produced_by(first.attempt.id)
    # Clearing the key: bindings stay; a third job's request at the key starts a fresh attempt.
    job_c, attempt_c = await running_root(database)
    async with database.transaction() as tx:
        assert await execution.clear_cache_entry(tx, "k")
    assert await request(database, job_c, attempt_c, "k", one) == RunIt(
        one.entity_id, replaces=None
    )
    fresh = await start_step(database, job_c, "k")
    assert fresh is not None and fresh.attempt.id != first.attempt.id
    for job_id in (job_a, job_b):
        assert isinstance(await step_status_of(database, job_id, "k"), Done)
    # A fourth job that requested the call but never took the result has nothing to take now.
    job_d, attempt_d = await running_root(database)
    assert isinstance(await request(database, job_d, attempt_d, "k", one), Pending)
    assert await take(database, attempt_d, "k") is None


async def test_every_waiting_job_takes_a_completed_result(database: Database) -> None:
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database)
    req = await a_request(database, 1)
    await request(database, job_a, attempt_a, "k", req)
    started = await start_step(database, job_a, "k")
    assert started is not None
    assert await request(database, job_b, attempt_b, "k", req) == Pending(started.attempt.id)
    ref = await complete(database, started.attempt.id, 3)
    for job_id, attempt_id in ((job_a, attempt_a), (job_b, attempt_b)):
        assert await step_status_of(database, job_id, "k") == Ready(produced_by(started.attempt.id))
        assert await pending(database, job_id) == []  # on offer, not to be run again
        assert await take(database, attempt_id, "k") is not None
        done = await step_status_of(database, job_id, "k")
        assert isinstance(done, Done) and done.result.entity_id == ref.entity_id
        async with database.transaction() as tx:
            binding = await ledger.get_step_binding(tx, job_id, "k")
        assert binding is not None and binding.step_result_id == produced_by(started.attempt.id)


async def test_a_pending_job_takes_over_an_abandoned_step_attempt(database: Database) -> None:
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database)
    req = await a_request(database, 1)
    await request(database, job_a, attempt_a, "k", req)
    started = await start_step(database, job_a, "k")
    assert started is not None
    assert await request(database, job_b, attempt_b, "k", req) == Pending(started.attempt.id)
    async with database.transaction() as tx:
        act = await execution.guard_step(tx, started.attempt.id, ending=True)
        await execution.close_step_attempt(tx, act, "abandoned", reason="runner stopped")
    # Abandoned, not failed: both jobs are due to run it again; whoever starts first wins.
    assert await pending(database, job_a) == [StartStep("k")]
    assert await pending(database, job_b) == [StartStep("k")]
    successor = await start_step(database, job_b, "k")
    assert successor is not None
    assert await step_status_of(database, job_a, "k") == Pending(successor.attempt.id)


# ---- cancellation ----


async def cancel(database: Database, job_id: JobID, *, hard: bool) -> None:
    async with database.transaction() as tx:
        job_attempt = await execution.active_job_attempt(tx, job_id)
        assert job_attempt is not None
        await execution.cancel(tx, job_attempt, hard=hard, reason="test")


async def test_a_soft_cancel_stops_deciding_and_leaves_running_work_alone(
    database: Database,
) -> None:
    job_id = await a_job(database)
    root = await start_root(database, job_id)
    _, (first, second) = await expand_children(database, root)
    first_started = await start_invocation(database, job_id, first)  # one child running, one due
    await cancel(database, job_id, hard=False)
    async with database.transaction() as tx:
        (job_attempt,) = await execution.list_job_attempts(tx, job_id)
        assert job_attempt.state == "cancelled" and job_attempt.reason == "test"
        assert job_attempt.settled_at is not None and not job_attempt.evicted
        with pytest.raises(NotCancellableError):
            await execution.cancel(tx, job_attempt, hard=False, reason="again")
        # The running attempts are untouched, and their lease still refreshes.
        assert (await execution.get_attempt(tx, first_started.attempt_id)).state == "running"
        assert (await execution.get_attempt(tx, root.attempt_id)).state == "running"
        assert await execution.heartbeat(tx, job_attempt.id, OWNER) == "cancelled"
        # Nothing unfinished is pending any more, running or not, and nothing is due.
        assert isinstance(await execution.status(tx, job_id, first), Stopped)
        assert isinstance(await execution.status(tx, job_id, second), Stopped)
        # The cancelled job attempt never started it and nothing more is due under it: stopped.
        assert (await execution.invocation_state(tx, second))[0] == "stopped"
        assert await execution.pending_work(tx, job_attempt) == []
    # A child closing under the cancelled job attempt still writes, and is not retried.
    await close(database, first_started.attempt_id, "failed", error=BOOM)
    assert [a.state for a in await attempts_of(database, job_id, first.pathname)] == ["failed"]
    # A resume starts the next job attempt, due to run the failed child and the never-started one.
    resumed = await resume(database, job_id)
    assert resumed.seq == 2
    assert set(await pending(database, job_id)) == {
        StartInvocation(first.id),
        StartInvocation(second.id),
    }
    assert isinstance(await status_of(database, job_id, first.pathname), Pending)


async def test_a_soft_cancel_stops_the_jobs_steps_until_a_resume(database: Database) -> None:
    """Without an active job attempt nothing of the job's steps is pending: a request nobody
    started is stopped rather than due, and so is the job's own step attempt while it drains. Once
    that attempt completes its result is on offer (the draining workflow attempt may still take
    it), and a resume makes the unstarted request due again."""
    job_id, root = await running_root(database)
    await request(database, job_id, root, "unstarted", await a_request(database, 1))
    await request(database, job_id, root, "draining", await a_request(database, 2))
    started = await start_step(database, job_id, "draining")
    assert started is not None
    await cancel(database, job_id, hard=False)
    for key in ("unstarted", "draining"):
        assert await step_status_of(database, job_id, key) == Stopped("no active job attempt")
    await complete(database, started.attempt.id, 2)
    assert await step_status_of(database, job_id, "draining") == Ready(
        produced_by(started.attempt.id)
    )
    await resume(database, job_id, "root")
    assert await pending(database, job_id) == [StartStep("unstarted")]
    assert await step_status_of(database, job_id, "unstarted") == Pending()


async def test_a_hard_cancel_evicts_and_another_job_takes_over_its_step(
    database: Database,
) -> None:
    job_a, attempt_a = await running_root(database)
    job_b, attempt_b = await running_root(database, owner="other")
    req, own = await a_request(database, 1), await a_request(database, 2)
    await request(database, job_a, attempt_a, "shared", req)
    await request(database, job_a, attempt_a, "mine", own)
    shared = await start_step(database, job_a, "shared")
    mine = await start_step(database, job_a, "mine")
    assert shared is not None and mine is not None
    assert await request(database, job_b, attempt_b, "shared", req) == Pending(shared.attempt.id)
    await cancel(database, job_a, hard=True)
    async with database.transaction() as tx:
        (job_attempt,) = await execution.list_job_attempts(tx, job_a)
        assert job_attempt.state == "cancelled" and job_attempt.evicted
        assert job_attempt.evicted_reason == "cancelled: test"
        assert await execution.heartbeat(tx, job_attempt.id, OWNER) is None
        for attempt_id in (attempt_a, shared.attempt.id, mine.attempt.id):
            abandoned = await execution.get_attempt(tx, attempt_id)
            assert abandoned.state == "abandoned" and abandoned.reason == "cancelled: test"
    # Nothing live holds the shared key any more: job B runs the step itself.
    assert await request(database, job_b, attempt_b, "shared", req) == RunIt(
        req.entity_id, replaces=shared.attempt.id
    )
    taken = await start_step(database, job_b, "shared")
    assert taken is not None
    ref = await complete(database, taken.attempt.id, 5)
    assert (await take(database, attempt_b, "shared")) is not None
    async with database.transaction() as tx:
        binding = await ledger.get_step_binding(tx, job_b, "shared")
    assert binding is not None and binding.result.entity_id == ref.entity_id
    # Job A's request is not bound: nothing of it runs to take the result. It is on offer, and
    # the next job attempt of A takes it when it replays the root.
    assert await step_status_of(database, job_a, "shared") == Ready(produced_by(taken.attempt.id))
