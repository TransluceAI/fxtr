"""Tests for the driver: the primary run, then a verification run if verification held the return,
over a recording executor and a fake attempt."""

from __future__ import annotations

from collections.abc import Collection

import pytest
from frozendict import frozendict
from runs_testing import FakeAttempt, identity

from fxtr.entity_defns.array import ArrayType
from fxtr.runner.assembly import WorkflowRun
from fxtr.runner.driver import drive_workflow_attempt, verification_run_id
from fxtr.runner.session import AttemptError, AttemptID, NodeToken, RunSession

pytestmark = pytest.mark.anyio

RUN = AttemptID("attempt-1")
INPUTS = frozendict({"x": (identity(0), ArrayType.of(("[k: int]", int)))})


class Body:
    """An executor standing in for a worker: runs a scripted body on each run it is handed and
    records the runs' ids and modes."""

    def __init__(self, *, raise_on: Collection[str] = ()) -> None:
        self.runs: list[tuple[AttemptID, str]] = []
        self.raise_on = raise_on

    async def __call__(self, session: RunSession) -> None:
        assert isinstance(session, WorkflowRun)
        self.runs.append((session.run_id, session.mode))
        if session.mode in self.raise_on:
            await session.fail(AttemptError("ValueError", "boom", ""))
            return
        session.emit_combine(NodeToken(1), {"a": NodeToken(0)}, "exact")
        await session.complete_workflow(NodeToken(1))


async def test_without_verification_the_body_runs_once() -> None:
    attempt, body = FakeAttempt(inputs=INPUTS), Body()
    await drive_workflow_attempt(attempt, body, run_id=RUN, verify_determinism=False)
    assert body.runs == [(RUN, "primary")]
    assert attempt.calls[-1][0] == "record_return"


async def test_verification_runs_the_body_again_and_records_once() -> None:
    attempt, body = FakeAttempt(inputs=INPUTS), Body()
    await drive_workflow_attempt(attempt, body, run_id=RUN, verify_determinism=True)
    assert body.runs == [(RUN, "primary"), (verification_run_id(RUN), "verification")]
    assert [c[0] for c in attempt.calls] == ["emit", "hold_return", "record_return"]
    assert attempt.verification_phase == "agreed"


async def test_a_failed_primary_run_gets_no_verification() -> None:
    attempt, body = FakeAttempt(inputs=INPUTS), Body(raise_on={"primary"})
    await drive_workflow_attempt(attempt, body, run_id=RUN, verify_determinism=True)
    assert body.runs == [(RUN, "primary")]
    assert attempt.ended and attempt.calls[-1] == ("fail", "ValueError", False)


async def test_a_verification_run_that_raises_fails_the_attempt() -> None:
    attempt, body = FakeAttempt(inputs=INPUTS), Body(raise_on={"verification"})
    await drive_workflow_attempt(attempt, body, run_id=RUN, verify_determinism=True)
    assert len(body.runs) == 2
    assert attempt.ended and attempt.calls[-1] == ("fail", "DeterminismError", False)
