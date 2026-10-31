"""Tests for the experimental worker loop: registration, the message stream, and how attempts are
stopped and reported, against a mocked multiplexed session."""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import anyio
import pytest
from frozendict import frozendict

from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.registry import FunctionRegistry
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.runner.experimental.multiplexed import (
    Assigned,
    AttemptCancelled,
    HostDisconnectedError,
    Shutdown,
    StepSpec,
    WorkerMessage,
    WorkerSession,
    WorkflowSpec,
)
from fxtr.runner.experimental.worker import Worker, function_specs
from fxtr.runner.session import (
    Assignment,
    AttemptID,
    NodeToken,
    StepAssignment,
    WorkflowAssignment,
)
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio

ATTEMPT = AttemptID("attempt-1")
SAMPLES = ArrayType.of(("[sample: int]", str))


@step(inputs={"prompt": str, "n": int}, output=SAMPLES)
async def sample(context: StepContext, prompt: str, n: int) -> Array[str]:
    return Array.from_items([((i,), f"{prompt}-{i}") for i in range(n)], SAMPLES)


@step(inputs={"samples": SAMPLES}, output=int)
async def count(context: StepContext, samples: Array[str]) -> int:
    return len(samples)


def make_session() -> tuple[Any, MemoryStorage]:
    """A mocked multiplexed session with a real in-memory store behind its storage methods."""
    session = create_autospec(WorkerSession, instance=True)
    storage = MemoryStorage()
    session.store_raw_data.side_effect = lambda attempt_id, data: storage.store_raw_data_sync(data)
    session.is_known.side_effect = lambda attempt_id, entity_id: storage.is_known(entity_id)

    async def load(attempt_id: AttemptID, entity_id: EntityID) -> Any:
        return await storage.load_raw_data(entity_id)

    session.load_raw_data.side_effect = load
    return session, storage


async def step_assignment(
    storage: MemoryStorage, s: Any, attempt_id: AttemptID = ATTEMPT, **inputs: Array[Any]
) -> StepAssignment:
    return StepAssignment(
        attempt_id=attempt_id,
        step_name=s.name,
        cache_key=f"cache/{s.name}",
        inputs=frozendict({name: await storage.store(t) for name, t in inputs.items()}),
    )


def workflow_assignment(
    w: Any, attempt_id: AttemptID = ATTEMPT, **inputs: Any
) -> WorkflowAssignment:
    return WorkflowAssignment(
        attempt_id=attempt_id,
        workflow_name=w.name,
        pathname="root/wf",
        cache_key_prefix="cache/wf",
        inputs=frozendict(
            {
                name: (NodeToken(i), ArrayType.of(spec))
                for i, (name, spec) in enumerate(inputs.items())
            }
        ),
    )


@workflow(inputs={"x": int}, output=int)
async def stuck(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    await anyio.sleep_forever()
    return x


class LoopHarness:
    """A mocked session whose message stream the test controls."""

    def __init__(self) -> None:
        self.session, self.storage = make_session()
        self._messages_send, messages = anyio.create_memory_object_stream[WorkerMessage](10)

        async def message_stream() -> Any:
            async with messages:
                async for message in messages:
                    yield message

        self.session.messages.side_effect = message_stream

    async def assign(self, *assignments: Assignment) -> None:
        for assignment in assignments:
            await self._messages_send.send(Assigned(assignment))
        await anyio.wait_all_tasks_blocked()

    async def cancel(self, attempt_id: AttemptID, reason: str = "superseded") -> None:
        await self._messages_send.send(AttemptCancelled(attempt_id, reason))
        await anyio.wait_all_tasks_blocked()

    async def stop(self) -> None:
        """Ask the worker to shut down, then end the stream."""
        await self._messages_send.send(Shutdown("test over"))
        await self._messages_send.aclose()

    async def disconnect(self) -> None:
        """End the stream without a Shutdown, as a lost connection would."""
        await self._messages_send.aclose()


def stuck_assignment(attempt_id: AttemptID = ATTEMPT) -> WorkflowAssignment:
    return workflow_assignment(stuck, attempt_id, x=int)


async def test_worker_registers_runs_assigned_attempts_and_stops_on_shutdown() -> None:
    h = LoopHarness()
    registry = FunctionRegistry([sample, count])
    worker = Worker(h.session, registry, max_concurrency=2)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(worker.run)
            await h.assign(
                await step_assignment(
                    h.storage,
                    sample,
                    AttemptID("a"),
                    prompt=Array.scalar("p", str),
                    n=Array.scalar(1, int),
                ),
                await step_assignment(
                    h.storage,
                    count,
                    AttemptID("b"),
                    samples=Array.from_items([((0,), "x")], SAMPLES),
                ),
            )
            completed = {call.args[0] for call in h.session.complete_step.await_args_list}
            assert completed == {AttemptID("a"), AttemptID("b")}
            await h.stop()
    h.session.register.assert_awaited_once_with(function_specs(registry), max_concurrency=2)
    h.session.fail.assert_not_awaited()
    h.session.abandon.assert_not_awaited()


async def test_worker_runs_whatever_it_is_assigned_concurrently() -> None:
    """Concurrency is the session's to enforce: the worker declares it and runs every assignment
    it receives, so three assignments to a worker declaring one run at once."""
    h = LoopHarness()
    worker = Worker(h.session, FunctionRegistry([stuck]), max_concurrency=1)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(worker.run)
            await h.assign(*(stuck_assignment(AttemptID(f"a{i}")) for i in range(3)))
            await h.cancel(AttemptID("a0"))
            await h.stop()
    h.session.register.assert_awaited_once_with(
        frozenset({WorkflowSpec(stuck.name)}), max_concurrency=1
    )
    # Two abandoned on stop; the session-cancelled one was not reported.
    assert {c.args[0] for c in h.session.abandon.await_args_list} == {
        AttemptID("a1"),
        AttemptID("a2"),
    }
    h.session.fail.assert_not_awaited()


async def test_host_cancellation_stops_the_attempt_silently() -> None:
    h = LoopHarness()
    worker = Worker(h.session, FunctionRegistry([stuck]), max_concurrency=1)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(worker.run)
            await h.assign(stuck_assignment())
            await h.cancel(ATTEMPT)
            await h.stop()
    h.session.fail.assert_not_awaited()
    h.session.abandon.assert_not_awaited()
    h.session.complete_workflow.assert_not_awaited()


async def test_shutdown_abandons_running_attempts() -> None:
    h = LoopHarness()
    worker = Worker(h.session, FunctionRegistry([stuck]), max_concurrency=1)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(worker.run)
            await h.assign(stuck_assignment())
            await h.stop()
    h.session.abandon.assert_awaited_once_with(ATTEMPT, "worker stopped")
    h.session.fail.assert_not_awaited()


async def test_disconnect_abandons_running_attempts_and_raises() -> None:
    h = LoopHarness()

    # The attempt itself cuts the stream once it is running, so no outer task group is needed
    # (which would wrap run()'s exception in an ExceptionGroup).
    @workflow(inputs={"x": int}, output=int)
    async def cut_the_line(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
        await h.disconnect()
        await anyio.sleep_forever()
        return x

    worker = Worker(h.session, FunctionRegistry([cut_the_line]), max_concurrency=1)
    await h.assign(workflow_assignment(cut_the_line, x=int))
    with anyio.fail_after(5), pytest.raises(HostDisconnectedError):
        await worker.run()
    h.session.abandon.assert_awaited_once_with(ATTEMPT, "worker stopped")
    h.session.fail.assert_not_awaited()


async def test_cancelling_the_worker_abandons_running_attempts() -> None:
    h = LoopHarness()
    worker = Worker(h.session, FunctionRegistry([stuck]), max_concurrency=1)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(worker.run)
            await h.assign(stuck_assignment())
            tg.cancel_scope.cancel()
    h.session.abandon.assert_awaited_once_with(ATTEMPT, "worker stopped")
    h.session.fail.assert_not_awaited()


async def test_abandon_report_is_bounded_when_the_host_hangs() -> None:
    h = LoopHarness()

    async def hang(attempt_id: AttemptID, reason: str) -> None:
        await anyio.sleep_forever()

    h.session.abandon.side_effect = hang
    worker = Worker(h.session, FunctionRegistry([stuck]), max_concurrency=1, abandon_timeout=0.05)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(worker.run)
            await h.assign(stuck_assignment())
            tg.cancel_scope.cancel()
    h.session.abandon.assert_awaited_once()


def test_a_registry_advertises_a_spec_per_function() -> None:
    assert function_specs(FunctionRegistry([sample, stuck])) == {
        StepSpec(sample.name),
        WorkflowSpec(stuck.name),
    }
