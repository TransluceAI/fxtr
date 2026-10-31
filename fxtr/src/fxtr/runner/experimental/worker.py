"""The worker loop for a multiplexed :class:`~fxtr.runner.experimental.multiplexed.WorkerSession`.

:class:`Worker` registers what it can run and how many attempts it will run at once
(:meth:`~fxtr.runner.experimental.multiplexed.WorkerSession.register`), then reads the host's
messages: each :class:`~fxtr.runner.experimental.multiplexed.Assigned` starts the attempt in the
worker's task group under its own cancel scope, on a run session bound to the assignment
(:func:`~fxtr.runner.experimental.multiplexed.bind_run`) and run by
:func:`~fxtr.runner.worker.run_assignment`. The host is responsible for never assigning more than
the declared concurrency; the worker runs whatever it is assigned. An attempt the host cancels
(:class:`~fxtr.runner.experimental.multiplexed.AttemptCancelled`) is simply stopped. An attempt
cancelled for any other reason (the host sent
:class:`~fxtr.runner.experimental.multiplexed.Shutdown`, its message stream broke, or the worker
itself was cancelled, say by an interrupt) is reported with
:meth:`~fxtr.runner.experimental.multiplexed.WorkerSession.abandon` before the cancellation
propagates, so that after a shutdown every attempt the worker was assigned has been completed,
failed, or abandoned. A message stream that ends without a ``Shutdown`` is a disconnect: the worker
stops the same way but raises
:class:`~fxtr.runner.experimental.multiplexed.HostDisconnectedError`.
"""

from __future__ import annotations

import anyio

from fxtr.experiment.registry import FunctionRegistry
from fxtr.experiment.steps import Step
from fxtr.runner.experimental.multiplexed import (
    Assigned,
    AttemptCancelled,
    FunctionSpec,
    HostDisconnectedError,
    Shutdown,
    StepSpec,
    WorkerSession,
    WorkflowSpec,
    bind_run,
)
from fxtr.runner.session import Assignment, AttemptID
from fxtr.runner.worker import run_assignment


def function_specs(registry: FunctionRegistry) -> frozenset[FunctionSpec]:
    """What a worker advertises for registry with
    :meth:`~fxtr.runner.experimental.multiplexed.WorkerSession.register`: a
    :class:`~fxtr.runner.experimental.multiplexed.StepSpec` per step and a
    :class:`~fxtr.runner.experimental.multiplexed.WorkflowSpec` per workflow."""
    return frozenset(
        StepSpec(fn.name) if isinstance(fn, Step) else WorkflowSpec(fn.name)
        for fn in registry.functions
    )


class Worker:
    """Runs the attempts a host assigns until told to stop.

    Args:
        session: The session to register with, receive assignments from, and report to.
        registry: The functions this worker can run.
        max_concurrency: How many attempts the worker is willing to run at once; declared to the
            host, which enforces it.
        abandon_timeout: Seconds to wait for each ``abandon`` report while shutting down. The
            report runs shielded from the cancellation that caused it, so this bound is what keeps
            an unreachable host from wedging the shutdown.
    """

    def __init__(
        self,
        session: WorkerSession,
        registry: FunctionRegistry,
        *,
        max_concurrency: int = 1,
        abandon_timeout: float = 5.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._session = session
        self._registry = registry
        self._max_concurrency = max_concurrency
        self._abandon_timeout = abandon_timeout
        # The cancel scope of each running attempt, so host cancellations can find it.
        self._running: dict[AttemptID, anyio.CancelScope] = {}

    async def run(self) -> None:
        """Register with the host, then run every attempt it assigns until it sends
        :class:`~fxtr.runner.experimental.multiplexed.Shutdown` (or this call is cancelled).

        On shutdown, running attempts are cancelled and abandoned before returning.

        Raises:
            HostDisconnectedError: If the host's message stream ended without a ``Shutdown``.
                Attempts are cancelled and abandoned as for a shutdown first.
        """
        await self._session.register(
            function_specs(self._registry), max_concurrency=self._max_concurrency
        )
        shutdown: Shutdown | None = None
        async with anyio.create_task_group() as tg:
            async for message in self._session.messages():
                match message:
                    case Assigned(assignment):
                        tg.start_soon(self._run_one, assignment)
                    case AttemptCancelled(attempt_id=attempt_id):
                        scope = self._running.get(attempt_id)
                        if scope is not None:
                            scope.cancel()
                    case Shutdown():
                        shutdown = message
                        break
            # Stop, whether asked to or cut off. Each running attempt abandons itself on the way
            # out.
            tg.cancel_scope.cancel()
        if shutdown is None:
            raise HostDisconnectedError("The host's message stream ended without a Shutdown")

    async def _run_one(self, assignment: Assignment) -> None:
        attempt_id = assignment.attempt_id
        with anyio.CancelScope() as scope:
            self._running[attempt_id] = scope
            try:
                await run_assignment(bind_run(self._session, assignment), self._registry)
            except anyio.get_cancelled_exc_class():
                # Cancelled through our own scope means the host asked (AttemptCancelled) and
                # already knows. Anything else is the worker going down: tell the host.
                if not scope.cancel_called:
                    with anyio.CancelScope(shield=True), anyio.move_on_after(self._abandon_timeout):
                        await self._session.abandon(attempt_id, "worker stopped")
                raise
            finally:
                del self._running[attempt_id]
