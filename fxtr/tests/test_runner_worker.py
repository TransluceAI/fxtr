"""Tests for the worker: its attempt runners, against a mocked session."""

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
from fxtr.runner.experimental.multiplexed import WorkerSession, bind_run
from fxtr.runner.session import (
    AttemptID,
    NodeToken,
    StepAssignment,
    WorkflowAssignment,
)
from fxtr.runner.worker import run_assignment, run_step_attempt, run_workflow_attempt
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


@step(inputs={"x": int}, output=int)
async def explode(context: StepContext, x: int) -> int:
    raise ValueError(f"cannot handle {x}")


@workflow(inputs={"prompt": str, "n": int}, output=int)
async def count_samples(
    context: WorkflowContext, prompt: ArrayHandle[str], n: ArrayHandle[int]
) -> ArrayHandle[int]:
    samples = context.run_step("sample", sample, {"prompt": prompt, "n": n})
    return context.run_step("count", count, {"samples": samples})


@workflow(inputs={"x": int}, output=int)
async def passthrough(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return x


@workflow(inputs={"x": int}, output=str)
async def lies_about_type(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[Any]:
    return x


def make_session() -> tuple[Any, MemoryStorage]:
    """A mocked session with a real in-memory store behind its storage methods."""
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


def failure(session: Any) -> Any:
    session.fail.assert_awaited_once()
    attempt_id, error = session.fail.await_args.args
    assert attempt_id == ATTEMPT
    return error


# ---- step attempts ----


async def test_step_attempt_loads_inputs_and_completes() -> None:
    session, storage = make_session()
    assignment = await step_assignment(
        storage, sample, prompt=Array.scalar("hi", str), n=Array.scalar(2, int)
    )
    await run_step_attempt(bind_run(session, assignment), sample)  # type: ignore[arg-type]
    session.complete_step.assert_awaited_once_with(
        ATTEMPT, Array.from_items([((0,), "hi-0"), ((1,), "hi-1")], SAMPLES)
    )
    session.fail.assert_not_awaited()


async def test_run_attempt_reports_failures() -> None:
    session, storage = make_session()
    registry = FunctionRegistry([explode, count])
    await run_assignment(
        bind_run(session, await step_assignment(storage, explode, x=Array.scalar(7, int))),
        registry,
    )
    error = failure(session)
    assert (error.type_name, error.message) == ("ValueError", "cannot handle 7")
    assert "cannot handle 7" in error.traceback
    session.complete_step.assert_not_awaited()

    session.fail.reset_mock()
    await run_assignment(
        bind_run(
            session,
            await step_assignment(
                storage, sample, prompt=Array.scalar("a", str), n=Array.scalar(1, int)
            ),
        ),
        registry,
    )
    assert failure(session).type_name == "RegistryError"


# ---- workflow attempts ----


async def test_workflow_attempt_runs_body_and_completes_with_returned_node() -> None:
    session, _ = make_session()
    assignment = workflow_assignment(count_samples, prompt=str, n=int)
    await run_workflow_attempt(bind_run(session, assignment), count_samples)  # type: ignore[arg-type]
    # The inputs are tokens 0 and 1 (the host's); the body's two steps get 2 and 3 (the
    # worker's), and the attempt completes with the token of the node the body returned.
    emitted = session.emit_run_step.call_args_list
    assert [call.args for call in emitted] == [(ATTEMPT, NodeToken(2)), (ATTEMPT, NodeToken(3))]
    assert emitted[0].kwargs["inputs"] == {"prompt": NodeToken(0), "n": NodeToken(1)}
    assert emitted[1].kwargs["inputs"] == {"samples": NodeToken(2)}
    session.complete_workflow.assert_awaited_once_with(ATTEMPT, NodeToken(3))
    session.fail.assert_not_awaited()


async def test_workflow_attempt_may_return_an_input_handle() -> None:
    session, _ = make_session()
    await run_workflow_attempt(
        bind_run(session, workflow_assignment(passthrough, x=int)),  # type: ignore[arg-type]
        passthrough,
    )
    session.complete_workflow.assert_awaited_once_with(ATTEMPT, NodeToken(0))


async def test_workflow_attempt_checks_result_type_and_inputs() -> None:
    session, _ = make_session()
    registry = FunctionRegistry([lies_about_type, passthrough])
    await run_assignment(bind_run(session, workflow_assignment(lies_about_type, x=int)), registry)
    error = failure(session)
    assert error.type_name == "SignatureError"
    assert "expected []: str" in error.message
    session.complete_workflow.assert_not_awaited()

    session.fail.reset_mock()
    await run_assignment(bind_run(session, workflow_assignment(passthrough, y=int)), registry)
    assert failure(session).type_name == "SignatureError"


async def test_cancellation_propagates_without_reporting_failure() -> None:
    session, _ = make_session()

    @workflow(inputs={"x": int}, output=int)
    async def stuck(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
        await anyio.sleep_forever()
        return x

    registry = FunctionRegistry([stuck])
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                run_assignment, bind_run(session, workflow_assignment(stuck, x=int)), registry
            )
            await anyio.wait_all_tasks_blocked()
            tg.cancel_scope.cancel()
    session.fail.assert_not_awaited()
    session.complete_workflow.assert_not_awaited()
