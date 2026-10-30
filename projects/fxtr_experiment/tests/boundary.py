"""Test helper: one run across the service boundary, without an executor.

The boundary tests exercise the task-fn adapter / run capability / wire
transit in isolation, driving a single run against a hand-built engine.
The real run orchestration (scope ownership, prologue, persistence,
terminal status) lives in `fxtr_experiment.executor`; this helper only
reproduces the minimal mount-resolve-invoke shape those tests need.
"""

from __future__ import annotations

from typing import Mapping

from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.service_cap_common import UnknownService

from fxtr_experiment_task.model import OutputSpec
from fxtr_experiment.ops_engine import OpsEngine
from fxtr_experiment_task.runner_api import ViewGrant, WorkflowTaskFnProto
from fxtr_experiment.task_service import RunCapabilityImpl


async def run_task_over_engine(
    task_fn_ref: ServiceRef[UnknownService],
    engine: OpsEngine,
    *,
    runner_scope: FxtrScope,
    args: Mapping[str, ViewGrant],
    internals: ViewGrant,
    outputs: "frozendict[str, OutputSpec]",
) -> dict[str, FxtrID | bytes]:
    """One boundary run: per-run scope, mounted capability, invoke.

    Args:
        task_fn_ref: The task-fn service; must be resolvable in
            ``runner_scope``.
        engine: The generation's ops engine.
        runner_scope: The scope the per-run scope includes.
        args: One grant per rule input.
        internals: The cell's internals grant.
        outputs: The declared outputs.

    Returns:
        One value per declared output (bare ids / frame wire bytes —
        closures die with the run scope; boundary tests only compare
        identities and payloads).
    """
    async with FxtrScope(name="wf3-test-run", isolated=True, include=[runner_scope]) as run_scope:
        cap_ref = await run_scope.mount(RunCapabilityImpl(engine))
        proxy = await task_fn_ref.replace_service_type(WorkflowTaskFnProto).resolve(run_scope)
        result = await proxy.run_task(cap_ref, dict(args), internals, outputs)
        return dict(result)
