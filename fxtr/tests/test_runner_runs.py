"""Tests for the shared runs as run sessions: token translation, dispatch by mode, the held return
of determinism verification, failure reporting, over a fake attempt."""

from __future__ import annotations

from typing import Any

import pytest
from frozendict import frozendict
from runs_testing import FakeAttempt, FakeStepAttempt, identity

from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.query import dim
from fxtr.entity_defns.query_plan import PlannedQuery
from fxtr.runner import nodes
from fxtr.runner.assembly import StepRun, WorkflowRun
from fxtr.runner.query_codec import encode_query
from fxtr.runner.session import (
    AttemptClosedError,
    AttemptError,
    AttemptID,
    NodeToken,
    StepAssignment,
)

pytestmark = pytest.mark.anyio

RUN = AttemptID("attempt-1")
PROMPTS = ArrayType.of(("[prompt: int]", str))
COUNTS = ArrayType.of(("[prompt: int]", int))
INPUTS = frozendict({"prompts": (identity(0), PROMPTS), "n": (identity(1), COUNTS)})

RENAMED = encode_query(PlannedQuery.over(0, PROMPTS).rename({"prompt": "p"}).plan)
"""A one-step query plan, as a query node carries it."""
FILTERED = encode_query(PlannedQuery.over(0, PROMPTS).filter(dim("prompt") > 0).plan)


def T(n: int) -> NodeToken:
    return NodeToken(n)


def emitted(attempt: FakeAttempt) -> list[nodes.Node]:
    return [call[1] for call in attempt.calls if call[0] == "emit"]


def test_start_seeds_the_assignment_from_the_attempt() -> None:
    attempt = FakeAttempt(inputs=INPUTS)
    run = WorkflowRun.start(attempt, "primary", run_id=RUN, verify_determinism=False)
    assert run.run_id == RUN
    assert run.assignment.attempt_id == RUN
    assert (run.assignment.workflow_name, run.assignment.pathname) == ("wf", "root")
    assert run.assignment.inputs == {"prompts": (T(0), PROMPTS), "n": (T(1), COUNTS)}
    assert run.assembler.resolve(T(1)) == identity(1)


async def test_a_primary_run_translates_tokens_and_records() -> None:
    attempt = FakeAttempt(inputs=INPUTS)
    run = WorkflowRun.start(attempt, "primary", run_id=RUN, verify_determinism=False)
    run.emit_query(T(2), [T(0)], RENAMED)
    run.emit_combine(T(3), {"a": T(2), "b": T(1)}, "exact")
    array = Array.from_items([((0,), "p")], PROMPTS)
    run.emit_source_array(T(4), "src", array)
    run.log_message("hello")
    assert emitted(attempt) == [
        nodes.Query(inputs=(identity(0),), plan=RENAMED),
        nodes.Combine(
            inputs=frozendict({"a": emitted(attempt)[0].content_id(), "b": identity(1)}),
            join="exact",
        ),
        nodes.source_array("src", array),
    ]
    assert ("log", "hello") in attempt.calls
    assert attempt.entities.is_known(nodes.source_array("src", array).array.entity_id)
    # Tokens are the run's: the new node is bound and resolves to its identity.
    assert run.assembler.resolve(T(3)) == emitted(attempt)[1].content_id()


async def test_observe_resolves_the_token() -> None:
    value = Array.from_items([((0,), 3)], COUNTS)
    attempt = FakeAttempt(inputs=INPUTS, values={identity(1): value})
    run = WorkflowRun.start(attempt, "primary", run_id=RUN, verify_determinism=False)
    assert run.assembler.resolve(T(1)) == identity(1)
    assert await run.observe(T(1)) is value
    assert attempt.calls[-1:] == [("observe", identity(1))]


async def test_completion_records_or_holds_the_return_by_strictness() -> None:
    attempt = FakeAttempt(inputs=INPUTS)
    run = WorkflowRun.start(attempt, "primary", run_id=RUN, verify_determinism=False)
    await run.complete_workflow(T(0))
    assert attempt.calls == [("record_return", identity(0))]

    verified = FakeAttempt(inputs=INPUTS)
    run = WorkflowRun.start(verified, "primary", run_id=RUN, verify_determinism=True)
    await run.complete_workflow(T(1))
    assert verified.calls == [("hold_return", identity(1))]
    assert verified.verification_phase == "verifying"


async def test_a_verification_run_checks_and_records_the_held_return_on_agreement() -> None:
    attempt = FakeAttempt(inputs=INPUTS)
    primary = WorkflowRun.start(attempt, "primary", run_id=RUN, verify_determinism=True)
    primary.emit_query(T(2), [T(0)], RENAMED)
    await primary.complete_workflow(T(2))
    recorded = list(attempt.calls)

    verification = WorkflowRun.start(
        attempt, "verification", run_id=AttemptID("v"), verify_determinism=True
    )
    verification.emit_query(T(9), [T(0)], RENAMED)  # its own token numbering, the same node
    verification.log_message("again")  # dropped
    await verification.complete_workflow(T(9))
    assert attempt.calls == [*recorded, ("record_return", emitted(attempt)[0].content_id())]
    assert attempt.verification_phase == "agreed"


async def test_a_verification_difference_fails_the_attempt_and_stops_the_body() -> None:
    attempt = FakeAttempt(inputs=INPUTS)
    primary = WorkflowRun.start(attempt, "primary", run_id=RUN, verify_determinism=True)
    await primary.complete_workflow(T(0))
    verification = WorkflowRun.start(
        attempt, "verification", run_id=AttemptID("v"), verify_determinism=True
    )
    with pytest.raises(AttemptClosedError, match="failed verification"):
        verification.emit_query(T(2), [T(0)], RENAMED)  # the body did not emit this the first time
    assert attempt.calls[-1] == ("fail", "DeterminismError", True)
    assert attempt.ended and attempt.verification_phase == "failed"
    # Everything further from either run is refused.
    with pytest.raises(AttemptClosedError):
        verification.emit_query(T(3), [T(0)], FILTERED)
    with pytest.raises(AttemptClosedError):
        primary.log_message("late")
    # A late report from the verification run adds nothing.
    await verification.fail(AttemptError("AttemptClosedError", "closed", ""))
    assert attempt.calls[-1] == ("fail", "DeterminismError", True)


async def test_a_body_raising_only_on_verification_is_a_determinism_violation() -> None:
    attempt = FakeAttempt(inputs=INPUTS)
    primary = WorkflowRun.start(attempt, "primary", run_id=RUN, verify_determinism=True)
    await primary.complete_workflow(T(0))
    verification = WorkflowRun.start(
        attempt, "verification", run_id=AttemptID("v"), verify_determinism=True
    )
    await verification.fail(AttemptError("ValueError", "boom", ""))
    assert attempt.calls[-2:] == [("flush",), ("fail", "DeterminismError", False)]
    assert attempt.verification_phase == "failed"


async def test_failure_and_abandonment_flush_first_and_are_idempotent() -> None:
    attempt = FakeAttempt(inputs=INPUTS)
    run = WorkflowRun.start(attempt, "primary", run_id=RUN, verify_determinism=False)
    await run.fail(AttemptError("ValueError", "boom", ""))
    assert attempt.calls == [("flush",), ("fail", "ValueError", False)]
    await run.abandon("late")  # closed: nothing
    assert len(attempt.calls) == 2

    other = FakeAttempt(inputs=INPUTS)
    run = WorkflowRun.start(other, "primary", run_id=RUN, verify_determinism=False)
    await run.abandon("worker stopped")
    assert other.calls == [("flush",), ("abandon", "worker stopped")]


async def test_storage_forwards_to_the_attempts_store_even_once_closed() -> None:
    attempt = FakeAttempt(inputs=INPUTS)
    run = WorkflowRun.start(attempt, "primary", run_id=RUN, verify_determinism=False)
    stored = await run.store_raw_data({"$type": "t.T", "v": 1})
    assert run.is_known(stored)
    attempt.abandon("stop")
    assert await run.load_raw_data(stored) == {"$type": "t.T", "v": 1}
    with pytest.raises(AttemptClosedError):
        await run.store_raw_data({"$type": "t.T", "v": 2})


async def test_a_step_run_forwards_to_its_attempt() -> None:
    assignment = StepAssignment(
        attempt_id=RUN, step_name="s", cache_key="root/s", inputs=frozendict()
    )
    attempt = FakeStepAttempt(assignment)
    run = StepRun(RUN, attempt)
    assert run.run_id == RUN and run.assignment is assignment
    run.log_message("working")
    result: Array[Any] = Array.scalar(1, int)
    await run.complete_step(result)
    assert attempt.calls == [("log", "working"), ("complete", result)]
    with pytest.raises(AttemptClosedError):
        run.log_message("late")
    await run.fail(AttemptError("ValueError", "boom", ""))  # closed: nothing
    assert len(attempt.calls) == 2

    failing = FakeStepAttempt(assignment)
    await StepRun(RUN, failing).fail(AttemptError("ValueError", "boom", ""))
    assert failing.calls == [("fail", "ValueError")]
