"""The host-side attempts' lifecycle: an attempt ends once, in memory, and its outcome is
closed to the ledger separately, idempotently, by whichever task can; a close cancelled in
flight leaves the outcome for the next; a close the ledger already has is adopted."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from frozendict import frozendict
from postgres_testing import (
    a_job,
    a_request,
    active,
    request,
    running_root,
    start_root,
    start_step,
)

from fxtr.entity_defns.array import Array
from fxtr.runner.session import AttemptError, StepAssignment

pytest.importorskip("sqlalchemy")

from fxtr.core.entity_data import EntityID  # noqa: E402
from fxtr.runner.postgres import execution  # noqa: E402
from fxtr.runner.postgres.attempts import (  # noqa: E402
    Abandoned,
    Attempt,
    Completed,
    Failed,
    Observe,
    StepAttempt,
)
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.entities import PostgresEntityStorage  # noqa: E402

pytestmark = pytest.mark.anyio

BOOM = AttemptError(type_name="RuntimeError", message="boom", traceback="")
NODE = EntityID(cid=b"\x01\x71\x12\x20" + bytes(32))


async def a_live_attempt(database: Database) -> tuple[Attempt, list[str]]:
    """A running root's host-side attempt, with a log of the callbacks it makes."""
    job_id = await a_job(database)
    started = await start_root(database, job_id)
    calls: list[str] = []
    attempt = Attempt.create(
        started.attempt_id,
        started.invocation,
        {},
        inputs={},
        spawns={},
        entities=PostgresEntityStorage(database),
        database=database,
        cancel_body=lambda: calls.append("cancel_body"),
        on_change=lambda a: calls.append("on_change"),
    )
    return attempt, calls


async def state_of(database: Database, attempt: Attempt | StepAttempt) -> Any:
    async with database.transaction() as tx:
        return await execution.get_attempt(tx, attempt.id)


async def test_the_first_outcome_wins_and_ending_is_synchronous(database: Database) -> None:
    attempt, calls = await a_live_attempt(database)
    assert attempt.outcome is None and not attempt.ended and not attempt.closed
    attempt.fail(BOOM, cancel_body=True)
    assert attempt.outcome == Failed(BOOM) and attempt.ended and not attempt.closed
    assert calls == ["on_change", "cancel_body"]
    attempt.abandon("too late")  # a later outcome changes nothing
    attempt.fail(AttemptError("X", "other", ""), cancel_body=True)
    assert attempt.outcome == Failed(BOOM) and calls == ["on_change", "cancel_body"]
    assert (await state_of(database, attempt)).state == "running"  # ended, not yet closed
    await attempt.close()
    assert attempt.closed
    row = await state_of(database, attempt)
    assert row.state == "failed" and row.error == BOOM
    await attempt.close()  # idempotent


async def test_a_persist_cancelled_in_flight_leaves_the_outcome_for_the_next(
    database: Database,
) -> None:
    """The write is held up by another transaction's lock on the attempt row (as an in-flight
    guard would), the closing task is cancelled, and a later close writes the outcome."""
    attempt, _ = await a_live_attempt(database)
    attempt.fail(BOOM, cancel_body=False)
    holding = database.transaction()
    tx1 = await holding.__aenter__()
    await execution.guard(tx1, attempt.id)  # holds the row until released
    async with anyio.create_task_group() as tg:
        tg.start_soon(attempt.close)
        await anyio.sleep(0.2)
        assert not attempt.closed  # blocked on the row
        tg.cancel_scope.cancel()
    assert not attempt.closed and attempt.outcome == Failed(BOOM)
    await holding.__aexit__(None, None, None)
    with anyio.fail_after(5):
        await attempt.close()
    assert attempt.closed
    row = await state_of(database, attempt)
    assert row.state == "failed" and row.error == BOOM


async def test_a_close_the_ledger_already_has_is_adopted(database: Database) -> None:
    """Someone else closed the attempt in the ledger (an eviction): closing finds the guard
    refusing and adopts that; the ledger's state stands."""
    attempt, _ = await a_live_attempt(database)
    async with database.transaction() as tx:
        job_attempt = await active(database, attempt.invocation.job_id)
        await execution.evict(tx, job_attempt, "taken over")
    attempt.abandon("the runner stopped")
    await attempt.close()
    assert attempt.closed and attempt.outcome == Abandoned("the runner stopped")
    row = await state_of(database, attempt)
    assert row.state == "abandoned" and row.reason == "taken over"


async def test_a_step_attempt_completes_or_fails_and_is_persisted_last(database: Database) -> None:
    job_id, root_attempt = await running_root(database)
    req = await a_request(database, 1)
    await request(database, job_id, root_attempt, "k", req)
    started = await start_step(database, job_id, "k")
    assert started is not None

    def step_attempt(record: execution.AttemptRecord) -> StepAttempt:
        assert record.cache_key is not None
        return StepAttempt(
            id=record.id,
            cache_key=record.cache_key,
            assignment=StepAssignment(
                attempt_id=record.id, step_name="s", cache_key=record.cache_key, inputs=frozendict()
            ),
            entities=PostgresEntityStorage(database),
            _database=database,
            _on_change=lambda a: None,
        )

    # Failed: ended synchronously, closed by the attempt's task.
    failing = step_attempt(started.attempt)
    failing.fail(BOOM)
    assert failing.outcome == Failed(BOOM) and not failing.closed
    failing.abandon("the runner stopped")  # the task's last word is a no-op
    await failing.close()
    row = await state_of(database, failing)
    assert row.state == "failed" and row.error == BOOM
    # Completed: the result's write is the close, ended and closed in one go.
    job_b, root_b = await running_root(database)
    await request(database, job_b, root_b, "k", req)
    started_b = await start_step(database, job_b, "k")
    assert started_b is not None
    completing = step_attempt(started_b.attempt)
    await completing.complete(Array.scalar(2, int))
    assert isinstance(completing.outcome, Completed) and completing.closed
    completing.fail(BOOM)  # a stale report after completion changes nothing
    row = await state_of(database, completing)
    assert row.state == "completed" and row.result is not None


async def test_an_observe_handed_over_after_the_attempt_ended_is_refused(
    database: Database,
) -> None:
    """The recorder commits a batch and publishes it without yielding, but the attempt can have
    ended while that transaction ran: ending is synchronous and closing is not. An observe landing
    then is refused, rather than queued for an evaluator that has stopped and leaving the body
    waiting on it for good."""
    attempt, _ = await a_live_attempt(database)
    live = Observe(NODE)
    attempt.pending_observes = [live]

    attempt.fail(BOOM, cancel_body=False)  # the body keeps running, so it can still be waiting
    assert live.done.is_set() and attempt.pending_observes == []

    late = Observe(NODE)
    attempt.add_observe(late)
    assert attempt.pending_observes == []  # not queued for an evaluator that has stopped
    assert late.done.is_set() and late.error is live.error  # refused for the same reason


async def test_an_observe_handed_over_while_the_attempt_lives_waits_for_the_evaluator(
    database: Database,
) -> None:
    attempt, _ = await a_live_attempt(database)
    item = Observe(NODE)
    attempt.add_observe(item)
    assert attempt.pending_observes == [item] and not item.done.is_set()
