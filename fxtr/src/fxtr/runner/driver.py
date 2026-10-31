"""The driver: how one workflow attempt's body gets executed, once or twice, through an executor.

Determinism verification (see :mod:`fxtr.runner.assembly`) needs a body to run twice: the
*primary* run, whose return the attempt parks instead of recording, and a *verification* run that
must do the same things before the parked return is recorded. Deciding that a second run happens,
and when, is the one piece of sequencing above a run, and it lives here, once, in
:func:`drive_workflow_attempt`, so that a run never starts runs.

How a run is executed is the **executor's** business, behind one seam: a :data:`RunExecutor` is
a callable that executes one run session until it has ended, however that happens. In process,
the executor runs the body in the current task (``run_assignment`` over the run, see
:mod:`fxtr.runner.worker`); a multiplexed host's executor sends the run's assignment to a worker
elsewhere and awaits its completion. The driver, the attempt task around it, and the runner are
written over the seam, so the two never differ in what a body's execution means.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fxtr.experiment.registry import GLOBAL_REGISTRY, FunctionRegistry
from fxtr.runner.assembly import WorkflowAttemptOps, WorkflowRun
from fxtr.runner.session import AttemptID, RunSession
from fxtr.runner.worker import run_assignment

type RunExecutor = Callable[[RunSession], Awaitable[None]]
"""Execute one run until it has ended: its completion call has returned, it reported ``fail`` or
``abandon``, or the host cancelled it. Raises only if the executor itself broke, never for a
failure of the body, which the run reports to its attempt."""


def verification_run_id(run_id: AttemptID) -> AttemptID:
    """The id a verification run gets, derived from its attempt's primary run id."""
    return AttemptID(f"{run_id}/verify")


async def drive_workflow_attempt(
    attempt: WorkflowAttemptOps,
    execute: RunExecutor,
    *,
    run_id: AttemptID,
    verify_determinism: bool,
) -> None:
    """Execute a workflow attempt's body: the primary run, then, if verification held the return,
    a verification run of the same attempt under :func:`verification_run_id`.

    Returns when the last run has ended; the attempt itself may stay open after that (its
    evaluator finishing it) or have ended already (the body failed, or the verification differed).
    A second run is started only if the attempt is still open and its phase is ``verifying``, so a
    body that failed, or an attempt that ended meanwhile, gets no verification run.

    Args:
        attempt: The attempt, as a backend's attempt object.
        execute: How to execute a run.
        run_id: The id of the primary run (the attempt's own id, by convention).
        verify_determinism: Whether the primary run holds its return for verification.
    """
    await execute(
        WorkflowRun.start(attempt, "primary", run_id=run_id, verify_determinism=verify_determinism)
    )
    if not attempt.ended and attempt.verification_phase == "verifying":
        await execute(
            WorkflowRun.start(
                attempt,
                "verification",
                run_id=verification_run_id(run_id),
                verify_determinism=verify_determinism,
            )
        )


def in_process_executor(registry: FunctionRegistry = GLOBAL_REGISTRY) -> RunExecutor:
    """The executor that runs a body in the current task: :func:`~fxtr.runner.worker.run_assignment`
    over the run with ``registry``."""

    async def execute(run: RunSession) -> None:
        await run_assignment(run, registry)

    return execute


__all__ = ["RunExecutor", "drive_workflow_attempt", "in_process_executor", "verification_run_id"]
