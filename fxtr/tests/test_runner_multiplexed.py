"""Tests for the multiplexed host: its executor face (credit, Assigned, the run's end,
cancellation), its WorkerSession face (forwarding, refusals), drain and shutdown, and the whole
path through a real Worker loop over a fake attempt."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from frozendict import frozendict
from runs_testing import FakeAttempt, FakeStepAttempt, identity

from fxtr.entity_defns.array import Array, ArrayType
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.registry import FunctionRegistry
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.runner.assembly import StepRun, WorkflowRun
from fxtr.runner.driver import drive_workflow_attempt
from fxtr.runner.experimental.multiplexed import (
    Assigned,
    AttemptCancelled,
    MultiplexedHost,
    NotAcceptingError,
    StepSpec,
    WorkerMessage,
    WorkflowSpec,
)
from fxtr.runner.experimental.worker import Worker
from fxtr.runner.session import (
    AttemptClosedError,
    AttemptError,
    AttemptID,
    NodeToken,
    StepAssignment,
)

pytestmark = pytest.mark.anyio

COUNTS = ArrayType.of(("[k: int]", int))
INPUTS = frozendict({"x": (identity(0), COUNTS)})
A1, A2 = AttemptID("a1"), AttemptID("a2")


def T(n: int) -> NodeToken:
    return NodeToken(n)


class Received:
    """Reads the host's message stream into a list as messages arrive."""

    def __init__(self, host: MultiplexedHost) -> None:
        self.host = host
        self.messages: list[WorkerMessage] = []

    async def pump(self) -> None:
        async for message in self.host.messages():
            self.messages.append(message)

    def kinds(self) -> list[str]:
        return [type(m).__name__ for m in self.messages]


async def registered(max_concurrency: int = 2, **kwargs: Any) -> tuple[MultiplexedHost, Received]:
    host = MultiplexedHost(**kwargs)
    await host.register([WorkflowSpec("wf"), StepSpec("s")], max_concurrency=max_concurrency)
    return host, Received(host)


async def test_execute_sends_the_assignment_and_returns_when_the_run_completes() -> None:
    host, received = await registered()
    attempt = FakeAttempt(inputs=INPUTS)
    run = WorkflowRun.start(attempt, "primary", run_id=A1, verify_determinism=False)
    async with anyio.create_task_group() as tg:
        tg.start_soon(received.pump)
        tg.start_soon(host.execute, run)
        await anyio.wait_all_tasks_blocked()
        assert received.messages == [Assigned(run.assignment)]
        assert host.room == 1
        # The worker acts through the WorkerSession face; the host forwards to the run.
        host.emit_combine(A1, T(1), {"a": T(0)}, "exact")
        await host.complete_workflow(A1, T(1))
        await anyio.wait_all_tasks_blocked()
        assert host.room == 2 and attempt.calls[-1][0] == "record_return"
        host.shutdown("done")
    assert received.kinds() == ["Assigned", "Shutdown"]


async def test_credit_bounds_what_is_assigned_at_once() -> None:
    host, received = await registered(max_concurrency=1)
    runs = [
        WorkflowRun.start(
            FakeAttempt(inputs=INPUTS), "primary", run_id=aid, verify_determinism=False
        )
        for aid in (A1, A2)
    ]
    async with anyio.create_task_group() as tg:
        tg.start_soon(received.pump)
        for run in runs:
            tg.start_soon(host.execute, run)
        await anyio.wait_all_tasks_blocked()
        assert received.kinds() == ["Assigned"] and host.room == -1  # one waiting for credit
        await host.complete_workflow(A1, T(0))
        await anyio.wait_all_tasks_blocked()
        assert [m.assignment.attempt_id for m in received.messages if isinstance(m, Assigned)] == [
            A1,
            A2,
        ]
        await host.complete_workflow(A2, T(0))
        await host.drain()
    assert received.kinds()[-1] == "Shutdown"


async def test_cancelling_an_execute_cancels_the_run_on_the_worker() -> None:
    host, received = await registered()
    run = WorkflowRun.start(
        FakeAttempt(inputs=INPUTS), "primary", run_id=A1, verify_determinism=False
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(received.pump)
        async with anyio.create_task_group() as inner:
            inner.start_soon(host.execute, run)
            await anyio.wait_all_tasks_blocked()
            inner.cancel_scope.cancel()
        await anyio.wait_all_tasks_blocked()
        assert isinstance(received.messages[-1], AttemptCancelled) and host.room == 2
        with pytest.raises(AttemptClosedError, match="Unknown"):
            host.log_message(A1, "late")
        host.shutdown("done")
    assert received.kinds() == ["Assigned", "AttemptCancelled", "Shutdown"]


async def test_cancelling_an_execute_still_waiting_sends_nothing() -> None:
    host, received = await registered(max_concurrency=1)
    first = WorkflowRun.start(
        FakeAttempt(inputs=INPUTS), "primary", run_id=A1, verify_determinism=False
    )
    waiting = WorkflowRun.start(
        FakeAttempt(inputs=INPUTS), "primary", run_id=A2, verify_determinism=False
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(received.pump)
        tg.start_soon(host.execute, first)
        async with anyio.create_task_group() as inner:
            inner.start_soon(host.execute, waiting)
            await anyio.wait_all_tasks_blocked()
            inner.cancel_scope.cancel()
        await anyio.wait_all_tasks_blocked()
        assert received.kinds() == ["Assigned"]  # the waiting run was never sent, so no cancel
        await host.complete_workflow(A1, T(0))
        host.shutdown("done")


async def test_cancelling_the_driver_stops_the_body_through_either_executor() -> None:
    """How a backend stops a body: cancel the scope the driver runs in. Through the host that
    sends AttemptCancelled for the run in flight and starts no verification run."""
    host, received = await registered()
    attempt = FakeAttempt(inputs=INPUTS)
    body_scope = anyio.CancelScope()

    async def drive() -> None:
        with body_scope:
            await drive_workflow_attempt(attempt, host.execute, run_id=A1, verify_determinism=True)

    async with anyio.create_task_group() as tg:
        tg.start_soon(received.pump)
        tg.start_soon(drive)
        await anyio.wait_all_tasks_blocked()
        body_scope.cancel()
        await anyio.wait_all_tasks_blocked()
        assert received.kinds() == ["Assigned", "AttemptCancelled"]
        assert attempt.verification_phase == "recording"  # no second run was started
        host.shutdown("done")


async def test_fail_and_abandon_forward_and_end_the_run() -> None:
    host, received = await registered()
    attempt, step_attempt = (
        FakeAttempt(inputs=INPUTS),
        FakeStepAttempt(
            StepAssignment(attempt_id=A2, step_name="s", cache_key="k", inputs=frozendict())
        ),
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(received.pump)
        tg.start_soon(
            host.execute, WorkflowRun.start(attempt, "primary", run_id=A1, verify_determinism=False)
        )
        tg.start_soon(host.execute, StepRun(A2, step_attempt))
        await anyio.wait_all_tasks_blocked()
        await host.fail(A1, AttemptError("ValueError", "boom", ""))
        await host.abandon(A2, "worker stopped")
        await anyio.wait_all_tasks_blocked()
        assert attempt.calls[-1] == ("fail", "ValueError", False)
        assert step_attempt.calls == [("abandon", "worker stopped")]
        assert host.room == 2
        await host.fail(A1, AttemptError("ValueError", "late", ""))  # unknown now: a no-op
        host.shutdown("done")


async def test_wrong_kind_and_unknown_runs_are_refused() -> None:
    host, received = await registered()
    step_attempt = FakeStepAttempt(
        StepAssignment(attempt_id=A2, step_name="s", cache_key="k", inputs=frozendict())
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(received.pump)
        tg.start_soon(host.execute, StepRun(A2, step_attempt))
        await anyio.wait_all_tasks_blocked()
        with pytest.raises(AttemptClosedError, match="step attempt"):
            host.emit_combine(A2, T(1), {"a": T(0)}, "exact")
        with pytest.raises(AttemptClosedError, match="Unknown"):
            await host.complete_step(A1, Array.scalar(1, int))
        await host.complete_step(A2, Array.scalar(1, int))
        host.shutdown("done")


async def test_drain_waits_for_outstanding_runs_and_the_backend() -> None:
    ready = False
    host, received = await registered(ready_to_shutdown=lambda: ready)
    run = WorkflowRun.start(
        FakeAttempt(inputs=INPUTS), "primary", run_id=A1, verify_determinism=False
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(received.pump)
        tg.start_soon(host.execute, run)
        await anyio.wait_all_tasks_blocked()
        await host.drain()
        assert not host.accepting
        with pytest.raises(NotAcceptingError):
            await host.execute(
                WorkflowRun.start(
                    FakeAttempt(inputs=INPUTS), "primary", run_id=A2, verify_determinism=False
                )
            )
        await host.complete_workflow(A1, T(0))
        await anyio.wait_all_tasks_blocked()
        assert "Shutdown" not in received.kinds()  # the backend is not ready
        ready = True
        host.maybe_finish_drain()
    assert received.kinds()[-1] == "Shutdown"


async def test_register_is_once_and_execute_waits_for_a_worker() -> None:
    host = MultiplexedHost()
    received = Received(host)
    run = WorkflowRun.start(
        FakeAttempt(inputs=INPUTS), "primary", run_id=A1, verify_determinism=False
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(received.pump)
        tg.start_soon(host.execute, run)
        await anyio.wait_all_tasks_blocked()
        assert received.messages == [] and host.room == 0
        await host.register([WorkflowSpec("wf")], max_concurrency=1)
        await anyio.wait_all_tasks_blocked()
        assert received.messages == [Assigned(run.assignment)]
        with pytest.raises(Exception, match="already has a registered"):
            await host.register([WorkflowSpec("wf")], max_concurrency=1)
        await host.complete_workflow(A1, T(0))
        host.shutdown("done")


@workflow(inputs={"x": ("[k: int]", int)}, output=("[copy: str, k: int]", int), name="wf")
async def wf(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    context.log("hi")
    return context.stack({"a": x}, dim="copy")


async def test_a_worker_over_the_host_runs_the_driver_end_to_end() -> None:
    """The whole path: the driver executes through the host, the Worker loop on the other end
    runs the body on a bound run session, and the verification run follows."""
    host = MultiplexedHost()
    attempt = FakeAttempt(inputs=INPUTS)
    async with anyio.create_task_group() as tg:
        tg.start_soon(Worker(host, FunctionRegistry([wf]), max_concurrency=2).run)
        await drive_workflow_attempt(attempt, host.execute, run_id=A1, verify_determinism=True)
        await host.drain()
    kinds = [c[0] for c in attempt.calls]
    assert kinds == ["log", "emit", "hold_return", "record_return"]
    assert attempt.verification_phase == "agreed"
    assert not host.accepting
