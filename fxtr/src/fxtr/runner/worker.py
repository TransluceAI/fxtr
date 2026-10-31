"""The worker: runs the step and workflow attempts a host hands out.

A worker holds a :class:`~fxtr.experiment.registry.FunctionRegistry` of the steps and workflows it
can run, by name (the process's :data:`~fxtr.experiment.registry.GLOBAL_REGISTRY` unless it is
handed another). Given a run's session (:data:`~fxtr.runner.session.RunSession`),
:func:`run_assignment` looks the function its assignment names up, builds the run's context (see
:mod:`fxtr.runner.contexts`), runs the user function, checks its result against the signature, and
reports the outcome to the
session: ``complete_step`` or ``complete_workflow`` on success, and ``fail`` with an
:class:`~fxtr.runner.session.AttemptError` on any exception. Cancellation is not an exception in
this sense: it propagates. This is the in-process executor's whole job (see
:mod:`fxtr.runner.driver`).

The worker runs each assignment exactly once and knows nothing of determinism verification: the
host may hand it a second assignment of the same invocation (a verification run; see
:mod:`fxtr.runner.assembly`) and it treats that like any other. A worker that runs many attempts
at once over one message stream, rather than one run in the host's own process, is
:class:`~fxtr.runner.experimental.worker.Worker`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from fxtr.experiment.registry import FunctionRegistry
from fxtr.experiment.steps import Step
from fxtr.experiment.workflows import Workflow
from fxtr.runner.contexts import (
    RunnerArrayHandle,
    RunnerStepContext,
    RunnerWorkflowContext,
    node_token_of,
)
from fxtr.runner.session import (
    AttemptError,
    NodeToken,
    RunSession,
    StepAssignment,
    StepRunSession,
    WorkflowAssignment,
    WorkflowRunSession,
)


async def run_step_attempt(session: StepRunSession, step: Step[Any, Any, Any]) -> None:
    """Run one step attempt to completion, reporting success to its session.

    Loads the input arrays through the run's context, calls
    :meth:`~fxtr.experiment.steps.Step.invoke` (which unwraps scalars and checks the result against
    the signature), and completes the attempt with the result. Exceptions propagate; see
    :func:`run_assignment` for failure reporting.
    """
    context = RunnerStepContext(session)
    inputs = {name: await context.load(ref) for name, ref in session.assignment.inputs.items()}
    result = await step.invoke(context, inputs)
    await session.complete_step(result)


async def run_workflow_attempt(
    session: WorkflowRunSession, workflow: Workflow[Any, Any, Any]
) -> None:
    """Run one workflow body to completion, reporting success to its session.

    Builds the run's context and handles to its input nodes, calls the body with them, checks that
    the returned handle belongs to this run and that its type is assignable to the signature's
    output type for these inputs, and completes the run with the returned node. Exceptions
    propagate; see :func:`run_assignment` for failure reporting.

    Args:
        session: The run's session.
        workflow: The workflow to run; must be the one the session's assignment names.

    Raises:
        SignatureError: If the inputs do not fit the workflow's signature, or the returned handle's
            type does not fit its output type.
        ForeignArrayHandleError: If the body returned a handle that is not one of this run's.
    """
    token = await _run_body(RunnerWorkflowContext(session), workflow, session.assignment)
    await session.complete_workflow(token)


async def _run_body(
    context: RunnerWorkflowContext,
    workflow: Workflow[Any, Any, Any],
    assignment: WorkflowAssignment,
) -> NodeToken:
    """Call the body once over ``context``, with handles to the assignment's inputs, and return
    the token of the node it returned. :meth:`~fxtr.experiment.workflows.Workflow.invoke` checks
    the inputs against the signature, retypes and converts them to what the body annotates, and
    turns the body's return into a handle (checking it against the output type, or registering a
    bare value as the ``$return`` source array)."""
    handles: Mapping[str, RunnerArrayHandle[Any]] = {
        name: RunnerArrayHandle(context, token, type)
        for name, (token, type) in assignment.inputs.items()
    }
    result = await workflow.invoke(context, handles)
    return node_token_of(context, result)


async def run_assignment(session: RunSession, registry: FunctionRegistry) -> None:
    """Run the assignment a run's session carries and report its outcome to the session.

    Dispatches on the assignment's kind to :func:`run_step_attempt` or
    :func:`run_workflow_attempt`. Any :class:`Exception` (a missing or mismatched function, a
    signature error, a failure in user code, a refused session call) is reported with
    ``fail`` and not re-raised. ``BaseException``\\ s that are not ``Exception``\\ s,
    cancellation in particular, propagate.

    Args:
        session: The run's session: what to run, and where to report.
        registry: Where the assignment's function is looked up.
    """
    # The assignment's kind says which of the two session protocols this run's session satisfies.
    try:
        match session.assignment:
            case StepAssignment(step_name=step_name):
                step_session = cast(StepRunSession, session)
                await run_step_attempt(step_session, registry.step(step_name))
            case WorkflowAssignment(workflow_name=workflow_name):
                workflow_session = cast(WorkflowRunSession, session)
                await run_workflow_attempt(workflow_session, registry.workflow(workflow_name))
    except Exception as exc:
        await session.fail(AttemptError.from_exception(exc))
