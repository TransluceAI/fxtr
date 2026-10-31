"""The backends the semantics reference (``test_semantics_reference.py``) runs on, behind one
backend-neutral contract.

:class:`Execution` is what a backend reports about one job that *succeeded*: the result and what ran
to produce it (the workflow invocations, the steps by cache key, the attempt counts, how many times
bodies observed). It says nothing about failures: how a failure surfaces is each backend's own
business and is tested with each backend. An executor whose job did not succeed raises.

Each entry of :data:`BACKENDS` is an async context manager yielding an :class:`Executor`; the
reference's ``execute`` fixture enters it for the test's duration, so a backend can keep resources
(the Postgres backend's throwaway schemas) open until the test has read everything it wants.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Any, Protocol

import anyio
import pytest
from remote_testing import run_worker_over_wire

from fxtr.core.entities import BoundID, FxtrEntityStorage
from fxtr.entity_defns.array import Array, ArrayType, Key
from fxtr.experiment.registry import FunctionRegistry
from fxtr.runner.experimental.multiplexed import MultiplexedHost
from fxtr.runner.steps import StepRequest

TEST_DATABASE_URL_ENV_VAR = "FXTR_TEST_DATABASE_URL"


@dataclasses.dataclass(frozen=True)
class WorkflowSummary:
    """One workflow invocation of a job.

    Attributes:
        workflow: The workflow's short name (the last component of its dotted name).
        prefix: Its default cache key prefix.
        parent: The invocation that requested it, as its pathname and the joint key the request
            was made at; ``None`` for the root.
        node_types: The array type of every node of its DAG, in creation order.
    """

    workflow: str
    prefix: str
    parent: tuple[str, Key] | None
    node_types: tuple[ArrayType, ...]


@dataclasses.dataclass(frozen=True)
class StepSummary:
    """One step the job ran, at its cache key.

    Attributes:
        step: The step's short name.
        requests: How many run nodes (at how many joint keys) asked for this key; more than one
            is a cache hit within the job.
        inputs: The arrays the step received, by argument name, as references to load through
            :attr:`Execution.storage`.
        result: The stored output array, to load through :attr:`Execution.storage`.
        result_type: The type of the array it produced.
    """

    step: str
    requests: int
    inputs: Mapping[str, BoundID[Array[Any]]]
    result: BoundID[Array[Any]]
    result_type: ArrayType


@dataclasses.dataclass(frozen=True)
class Execution:
    """What a backend reports about one job that succeeded.

    Attributes:
        result: The root workflow's result.
        storage: An entity store holding everything the job stored, to load references through.
        workflows: Every workflow invocation, by pathname.
        steps: Every step, by cache key.
        attempts: How many attempts ran, by kind (``"workflow"`` and ``"step"``).
        observes: How many times workflow bodies observed a node's value.
    """

    result: Array[Any]
    storage: FxtrEntityStorage
    workflows: dict[str, WorkflowSummary]
    steps: dict[str, StepSummary]
    attempts: dict[str, int]
    observes: int


class Executor(Protocol):
    """Runs a root workflow, with the given steps and workflows available, on one backend, and
    reports the execution. Raises if the job did not succeed."""

    async def __call__(
        self,
        root: Any,
        *functions: Any,
        inputs: Mapping[str, Array[Any]] = {},
    ) -> Execution: ...


def _short(function_name: str) -> str:
    return function_name.rsplit(".", 1)[-1]


# ---- the Postgres backend ----


@asynccontextmanager
async def postgres_backend(
    *, remote: bool = False, max_concurrency: int = 32
) -> AsyncIterator[Executor]:
    """An executor running each job on its own throwaway schema of the server named by
    ``FXTR_TEST_DATABASE_URL``, the way a launcher does: through a ``LocalProjectClient`` (over a
    fake environment, so no checkout is pinned) whose ``submit`` runs the job in this process. The
    schemas stay available until the context exits, so the execution's storage can be read
    after the call. With ``remote=True``, a ``JobRunner`` dispatches attempts through a
    multiplexed host and wire worker. Skips when no test database is configured."""
    url = os.environ.get(TEST_DATABASE_URL_ENV_VAR)
    if url is None:
        pytest.skip(f"{TEST_DATABASE_URL_ENV_VAR} is not set")
    pytest.importorskip("sqlalchemy")
    from postgres_testing import a_client, a_code_version

    from fxtr.runner.postgres import execution
    from fxtr.runner.postgres.database import Database
    from fxtr.runner.postgres.runner import JobRunner, RunLimits, RunnerSettings

    settings = RunnerSettings(heartbeat_interval=0.5, reconcile_interval=1.0, poll_interval=0.05)

    async with AsyncExitStack() as stack:

        async def execute(
            root: Any,
            *functions: Any,
            inputs: Mapping[str, Array[Any]] = {},
        ) -> Execution:
            database = await stack.enter_async_context(
                Database(url, pg_schema=f"ref_{uuid.uuid4().hex}")
            )
            await database.create_schema()
            stack.push_async_callback(database.drop_schema)
            # The client's task group must be left in the task that entered it, so the client
            # is used here and not held on the stack; the execution's storage reads through the
            # database afterwards (a disposed pool reconnects), until the schema is dropped.
            async with a_client(
                database,
                root,
                *functions,
                settings=settings,
                verify_determinism=True,
                limits=RunLimits(steps=max_concurrency),
            ) as client:
                with anyio.fail_after(8):
                    if remote:
                        job = await client.jobs.create(root, dict(inputs))
                        code_version = await a_code_version(database)
                        async with database.transaction() as tx:
                            attempt = await execution.resume(
                                tx, job, owner="reference-worker", code_version=code_version
                            )
                        assert isinstance(attempt, execution.JobAttemptRecord)
                        host = MultiplexedHost()
                        runner = JobRunner(
                            database,
                            attempt,
                            execute=host.execute,
                            settings=settings,
                            limits=RunLimits(steps=max_concurrency),
                        )
                        async with anyio.create_task_group() as tg:
                            tg.start_soon(
                                run_worker_over_wire, host, FunctionRegistry([root, *functions])
                            )
                            await runner.run()
                            await host.drain()
                        status = await (await client.job(job)).status()
                    else:
                        status = await client.run(root, dict(inputs))
                if status.state != "succeeded":
                    raise JobDidNotSucceed(
                        f"job {status.job_id} stopped: invocations {status.errors},"
                        f" steps {status.step_errors}"
                    )
                return await execution_from_job(client, status.job_id)

        yield execute


class JobDidNotSucceed(Exception):
    """The Postgres executor's job stopped without a result."""


async def execution_from_job(client: Any, job: Any) -> Execution:
    """The contract's view of a succeeded Postgres job, from its description."""
    described = await client.jobs.describe(job)
    by_id = {d.invocation.id: path for path, d in described.invocations.items()}
    parents: dict[str, tuple[str, Key]] = {}
    requests: dict[str, int] = {}
    observes = 0
    attempts = {"workflow": 0, "step": 0}
    for path, inv in described.invocations.items():
        for spawn in inv.spawns:
            if spawn.child_invocation_id is not None:
                parents[by_id[spawn.child_invocation_id]] = (path, spawn.key)
            else:
                assert spawn.step_cache_key is not None
                requests[spawn.step_cache_key] = requests.get(spawn.step_cache_key, 0) + 1
        attempts["workflow"] += len(inv.attempts)
        observes += sum(1 for a in inv.attempts for e in a.events if e.kind == "observe")
    steps: dict[str, StepSummary] = {}
    for key, step in described.steps.items():
        assert step.binding is not None and step.attempt is not None
        request = await client.entities.load(step.request.request.bind_type(StepRequest))
        result = await client.entities.load(step.binding.result)
        steps[key] = StepSummary(
            step=_short(step.spec.step_name),
            requests=requests.get(key, 0),
            inputs=request.inputs,
            result=step.binding.result,
            result_type=result.type,
        )
        attempts["step"] += 1
    return Execution(
        result=await client.jobs.result(job),
        storage=client.entities,
        workflows={
            path: WorkflowSummary(
                workflow=_short(inv.invocation.workflow_name),
                prefix=inv.invocation.cache_key_prefix,
                parent=parents.get(path),
                node_types=tuple(n.type for n in inv.nodes),
            )
            for path, inv in described.invocations.items()
        },
        steps=steps,
        attempts=attempts,
        observes=observes,
    )


BACKENDS: dict[str, Callable[[], AbstractAsyncContextManager[Executor]]] = {
    "remote_postgres": partial(postgres_backend, remote=True),
    "postgres": postgres_backend,
}
"""The backends every reference example runs on, by name. A backend that cannot run in the
current environment skips inside its context manager (``pytest.skip``)."""

__all__ = [
    "BACKENDS",
    "Execution",
    "Executor",
    "StepSummary",
    "WorkflowSummary",
    "execution_from_job",
    "postgres_backend",
]
